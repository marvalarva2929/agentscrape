"""Drive the per-site pipeline for one site.

Used by the CLI (one site, no concurrency) and by each orchestrator worker.
Isolation lives here: any exception from a site is caught, recorded on its
SiteRun and turned into a failed status, so one bad site can never take down a
run or another site.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import update

from ..browser.fetcher import Fetcher
from ..config import settings
from ..db.enums import SiteRunStatus
from ..db.models import Site, SiteRun
from ..db.repositories.sites import upsert_site
from ..db.session import get_sessionmaker
from ..llm.usage import LLMUnavailable, UsageMeter
from ..orchestrator.events import EventEmitter, EventType, NullEmitter
from ..urls import canonicalize, entry_url, host_of
from .deps import PipelineDeps
from .graph import build_site_graph
from .state import SiteState, initial_state

log = logging.getLogger("agentscrape.runner")

# One graph step per node visit; the extract loop dominates. Leave generous
# headroom so a large budget never trips LangGraph's recursion guard.
def _recursion_limit(step_budget: int) -> int:
    from .nodes.extract import BATCH_SIZE

    # Duplicates and already-claimed pages cost no step, and gap filling adds
    # loops, so the node count is not bounded by budget / batch alone.
    return max(200, (step_budget // max(BATCH_SIZE, 1)) * 6 + 400)


async def ensure_site_run(
    *, run_id: str, site_url: str, force_rescan: bool = False,
    step_budget: int | None = None,
) -> tuple[str, str, str]:
    """Create (or reuse) the Site and its SiteRun row. Returns ids and the root url."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        site = await upsert_site(session, site_url)
        existing = await session.scalar(
            SiteRun.__table__.select().where(
                SiteRun.run_id == run_id, SiteRun.site_id == site.id
            )
        )
        if existing is not None:
            await session.commit()
            return site.id, existing.id, site.canonical_url

        site_run = SiteRun(
            run_id=run_id,
            site_id=site.id,
            status=SiteRunStatus.PENDING,
            force_rescan=force_rescan,
            step_budget=step_budget or settings.default_step_budget,
        )
        session.add(site_run)
        await session.commit()
        return site.id, site_run.id, site.canonical_url


async def run_site(
    *,
    site_id: str,
    site_run_id: str,
    root_url: str,
    run_id: str | None = None,
    agent_id: str = "agent-0",
    force_rescan: bool = False,
    skip_threshold: float | None = None,
    step_budget: int | None = None,
    allowed_domains: list[str] | None = None,
    browser_context=None,
    emitter: EventEmitter | None = None,
    should_stop=None,
    fetcher: Fetcher | None = None,
    crawl_strategy: str | None = None,
    modes: list[str] | None = None,
) -> SiteState:
    """Run one site end to end. Never raises: failures are recorded and returned."""
    emitter = emitter or NullEmitter()
    sessionmaker = get_sessionmaker()
    budget = step_budget or settings.default_step_budget
    threshold = (
        skip_threshold if skip_threshold is not None else settings.default_skip_threshold
    )
    canonical = canonicalize(root_url) or root_url
    domain = host_of(canonical)

    async with sessionmaker() as session:
        if allowed_domains is None:
            # Domains the school sheet says this institution also publishes on.
            site_row = await session.get(Site, site_id)
            allowed_domains = list((site_row.affiliated_domains if site_row else None) or [])
        await session.execute(
            update(SiteRun)
            .where(SiteRun.id == site_run_id)
            .values(
                status=SiteRunStatus.RUNNING,
                agent_id=agent_id,
                started_at=datetime.now(UTC),
                heartbeat_at=datetime.now(UTC),
                attempt=SiteRun.attempt + 1,
            )
        )
        await session.commit()

    await emitter.emit(
        EventType.SITE_STARTED,
        site_id=site_id, site_run_id=site_run_id, domain=domain, agent_id=agent_id,
    )

    state = initial_state(
        site_id=site_id,
        site_run_id=site_run_id,
        root_url=entry_url(canonical),
        root_domain=domain,
        allowed_domains=allowed_domains,
        run_id=run_id,
        agent_id=agent_id,
        force_rescan=force_rescan,
        skip_threshold=threshold,
        step_budget=budget,
        crawl_strategy=crawl_strategy,
        modes=modes,
    )

    meter = UsageMeter(scope=site_run_id)
    owns_fetcher = fetcher is None

    try:
        if owns_fetcher:
            fetcher = Fetcher()
            await fetcher.__aenter__()

        deps = PipelineDeps(
            fetcher=fetcher,
            sessionmaker=sessionmaker,
            browser_context=browser_context,
            emitter=emitter,
            meter=meter,
            should_stop=should_stop,
        )
        graph = build_site_graph(deps)

        # A pathological site must never hold its slot indefinitely.
        final: SiteState = await asyncio.wait_for(
            graph.ainvoke(state, config={"recursion_limit": _recursion_limit(budget)}),
            timeout=settings.site_timeout_seconds,
        )
        return final

    except LLMUnavailable as exc:
        log.error("site %s aborted: model unavailable (%s)", domain, exc)
        return await _fail(
            state, site_run_id, "LLM_UNAVAILABLE", str(exc)[:500], emitter, meter,
        )
    except TimeoutError:
        return await _fail(
            state, site_run_id, "SITE_TIMEOUT",
            f"site exceeded {settings.site_timeout_seconds}s and was abandoned",
            emitter, meter,
        )
    except asyncio.CancelledError:
        await _mark(site_run_id, SiteRunStatus.CANCELLED, meter)
        raise
    except Exception as exc:  # one site's failure must not affect any other
        log.exception("site %s failed", domain)
        return await _fail(
            state, site_run_id, "PIPELINE_ERROR", f"{type(exc).__name__}: {exc}"[:500],
            emitter, meter,
        )
    finally:
        if owns_fetcher and fetcher is not None:
            await fetcher.__aexit__(None, None, None)


async def _fail(
    state: SiteState, site_run_id: str, code: str, message: str,
    emitter: EventEmitter, meter: UsageMeter,
) -> SiteState:
    await _mark(site_run_id, SiteRunStatus.FAILED, meter, code=code, message=message)
    await emitter.emit(
        EventType.SITE_FAILED,
        site_id=state["site_id"], site_run_id=site_run_id,
        domain=state["root_domain"], error_code=code, reason=message,
    )
    return {**state, "status": "failed", "terminated": True,
            "error_code": code, "error_message": message}


async def _mark(
    site_run_id: str, status: SiteRunStatus, meter: UsageMeter,
    *, code: str | None = None, message: str | None = None,
) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(SiteRun)
            .where(SiteRun.id == site_run_id)
            .values(
                status=status, error_code=code, error_message=message,
                finished_at=datetime.now(UTC),
                tokens_in=meter.total.input_tokens,
                tokens_out=meter.total.output_tokens,
                spend_usd=meter.cost_usd,
            )
        )
        await session.commit()
