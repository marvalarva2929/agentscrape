"""Run lifecycle: CSV parsing, preview, creation, launch, cancel, retry."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.enums import (
    TERMINAL_SITE_RUN_STATUSES,
    RunStatus,
    SiteRunStatus,
    ValidationStatus,
)
from ..db.models import KnownPath, Record, Run, Site, SiteRun
from ..db.repositories.sites import upsert_site
from ..db.session import get_sessionmaker
from ..domain.schemas import CsvRowPreview, RunCreate, ValidatePreview
from ..urls import canonicalize, host_of
from ..validation.institution import classify_domain
from .limits import MemoryCeilingExceeded, RunLimits, check_memory_ceiling
from .events import EventEmitter, EventType
from .pool import RunOrchestrator, register, unregister

log = logging.getLogger("agentscrape.runs")

# asyncio keeps only a weak reference to a running task, so an orchestrator
# launched fire-and-forget could be garbage collected mid-run.
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# Column names that plausibly hold the institution URL.
URL_COLUMNS = ("url", "website", "site", "link", "domain", "homepage", "hospital_url")


def parse_csv(content: bytes | str) -> list[tuple[int, str]]:
    """Extract (row_number, raw_value) pairs from an uploaded CSV.

    Accepts a header row with a recognizable URL column, or a bare one-column
    list with no header at all — clients produce both.
    """
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    text = text.strip()
    if not text:
        return []

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []

    header = [c.strip().lower() for c in rows[0]]
    url_index = next(
        (i for i, name in enumerate(header) if name in URL_COLUMNS), None
    )
    if url_index is None and any("http" in c or "." in c for c in header):
        # No header: the first row is data.
        url_index, body = 0, rows
    else:
        url_index = url_index if url_index is not None else 0
        body = rows[1:]

    out: list[tuple[int, str]] = []
    for offset, row in enumerate(body, start=1):
        if not row:
            continue
        value = (row[url_index] if url_index < len(row) else "").strip()
        if value:
            out.append((offset, value))
    return out


def normalize_site_input(raw: str) -> str | None:
    candidate = raw.strip().strip('"\'')
    if not candidate:
        return None
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    canonical = canonicalize(candidate)
    if not canonical or not host_of(canonical) or "." not in host_of(canonical):
        return None
    return canonical


async def preview_csv(
    session: AsyncSession, entries: list[tuple[int, str]]
) -> ValidatePreview:
    """Per-row preview so the client can review before committing compute."""
    rows: list[CsvRowPreview] = []

    for row_number, raw in entries:
        url = normalize_site_input(raw)
        if url is None:
            rows.append(
                CsvRowPreview(
                    row=row_number, input=raw, url=None, valid=False,
                    error="Could not parse a hostname from this value.",
                )
            )
            continue

        domain = host_of(url)
        verdict = classify_domain(url)
        if verdict is not None and verdict.status == ValidationStatus.K12_REJECTED:
            rows.append(
                CsvRowPreview(
                    row=row_number, input=raw, url=url, valid=False,
                    error=verdict.reason,
                    validation_status=str(verdict.status),
                    validation_reason=verdict.reason,
                )
            )
            continue

        site = await session.scalar(select(Site).where(Site.root_domain == domain))
        if site is None:
            rows.append(CsvRowPreview(row=row_number, input=raw, url=url, valid=True))
            continue

        path_count = int(
            await session.scalar(
                select(func.count(KnownPath.id)).where(
                    KnownPath.site_id == site.id, KnownPath.is_active.is_(True)
                )
            ) or 0
        )
        record_count = int(
            await session.scalar(
                select(func.count(Record.id)).where(Record.site_id == site.id)
            ) or 0
        )

        rows.append(
            CsvRowPreview(
                row=row_number, input=raw, url=url, valid=True, site_id=site.id,
                known_site=True, last_scraped_at=site.last_scraped_at,
                known_path_count=path_count, previous_record_count=record_count,
                validation_status=site.validation_status,
                validation_reason=site.validation_reason,
            )
        )

    return ValidatePreview(
        total_rows=len(rows),
        valid_rows=sum(1 for r in rows if r.valid),
        invalid_rows=sum(1 for r in rows if not r.valid),
        known_sites=sum(1 for r in rows if r.known_site),
        rejected_sites=sum(
            1 for r in rows if r.validation_status == str(ValidationStatus.K12_REJECTED)
        ),
        rows=rows,
    )


class DirectorySearchUnavailable(ValueError):
    """Directory search was asked for on schools that cannot have it."""

    def __init__(self, problems: dict[str, str]) -> None:
        self.problems = problems
        super().__init__(
            "Directory search needs a school that has already been crawled and has a "
            "directory link: " + "; ".join(f"{k}: {v}" for k, v in problems.items())
        )


async def directory_search_problems(
    session: AsyncSession, sites: list[str], *, with_crawl: bool = False
) -> dict[str, str]:
    """Why each input cannot be directory-searched; empty when all can. With
    `with_crawl` the same run crawls first, so no stored people are needed."""
    problems: dict[str, str] = {}
    for raw in sites:
        url = normalize_site_input(raw)
        if url is None:
            continue
        site = await session.scalar(select(Site).where(Site.root_domain == host_of(url)))
        if site is None:
            problems[raw] = (
                "not in the school sheet" if with_crawl else "not crawled yet"
            )
            continue
        if with_crawl:
            if not site.directory_url:
                problems[raw] = "no directory link in the school sheet"
            continue
        records = int(
            await session.scalar(select(func.count(Record.id)).where(Record.site_id == site.id))
            or 0
        )
        if not records:
            problems[raw] = "not crawled yet"
        elif not site.directory_url:
            problems[raw] = "no directory link in the school sheet"
    return problems


async def create_run(session: AsyncSession, body: RunCreate) -> Run:
    """Create the Run and one SiteRun per valid site. Does not start it."""
    config = body.config
    # Reject before anything is written, so the client gets a clean error.
    check_memory_ceiling(config.concurrency)
    if "directory" in config.modes:
        problems = await directory_search_problems(
            session, body.sites, with_crawl="crawl" in config.modes
        )
        if problems:
            raise DirectorySearchUnavailable(problems)

    from .scheduler import next_rank

    # Every run waits its turn: two crawls at once split the one model budget
    # and each still pays its own startup. `queued` in the request is ignored.
    run_config = config.model_dump()
    run_config["queued"] = True
    run = Run(
        status=RunStatus.PENDING,
        label=config.label,
        config=run_config,
        queued=True,
        queue_rank=await next_rank(session),
        sites_total=0,
    )
    session.add(run)
    await session.flush()

    created = 0
    duplicates = 0
    # A run has one SiteRun per site. Several inputs can resolve to the same
    # institution ("uni.edu", "www.uni.edu/", "https://uni.edu/index.html"), and
    # without collapsing them the unique constraint would fail the whole run.
    seen_site_ids: set[str] = set()
    school_names: list[str] = []

    for raw in body.sites:
        url = normalize_site_input(raw)
        if url is None:
            log.warning("skipping unparseable site input %r", raw)
            continue
        site = await upsert_site(session, url)
        if site.id in seen_site_ids:
            duplicates += 1
            log.info("collapsing duplicate input %r onto site %s", raw, site.root_domain)
            continue
        seen_site_ids.add(site.id)
        school_names.append(site.name or site.hospital_name or site.root_domain)

        session.add(
            SiteRun(
                run_id=run.id,
                site_id=site.id,
                status=SiteRunStatus.PENDING,
                step_budget=config.step_budget,
                # Crawl order: schools made together share one created_at.
                position=created,
            )
        )
        created += 1

    run.sites_total = created
    if not (run.label or "").strip() and school_names:
        run.label = default_label(school_names, datetime.now(UTC))
    await session.commit()
    log.info(
        "created run %s with %d sites (%d duplicate inputs collapsed)",
        run.id, created, duplicates,
    )
    return run


def default_label(school_names: list[str], when: datetime) -> str:
    """A name for a crawl nobody named: the school, and when it was queued."""
    head = school_names[0]
    if len(school_names) > 1:
        head = f"{head} + {len(school_names) - 1} more"
    return f"{head} — {when:%d %b %Y}"


async def resume_interrupted_runs() -> list[str]:
    """Bring the queue back after a restart, starting exactly one run.

    The server is stopped on demand, so an in-flight run is normal, not an
    error. Its schools go back to waiting and each continues from its
    checkpoint. Nothing is relaunched here beyond the next run in line: a run
    from before runs were all queued joins the back of the queue, in the order
    it was created, so a restart cannot release several at once.

    A run still marked running whose heartbeat is fresh belongs to a process
    that is alive; `start_next_if_idle` leaves it alone and the supervisor
    picks the queue up once that heartbeat stops.
    """
    from .scheduler import next_rank, start_next_if_idle

    async with get_sessionmaker()() as session:
        legacy = list(
            (
                await session.execute(
                    select(Run)
                    .where(
                        Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING]),
                        Run.queued.is_(False),
                    )
                    .order_by(Run.created_at, Run.id)
                )
            ).scalars()
        )
        for run in legacy:
            log.info("queueing interrupted run %s behind the others", run.id)
            run.queued = True
            run.queue_rank = await next_rank(session)
            run.config = {**(run.config or {}), "queued": True}
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.PENDING
            await session.flush()
        await session.commit()

    started = await start_next_if_idle()
    return [started] if started is not None else []


async def launch_run(run_id: str, *, use_browser: bool = True) -> RunOrchestrator:
    """Start the orchestrator for a run as a background task."""
    async with get_sessionmaker()() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise ValueError(f"no run {run_id}")
        config = dict(run.config or {})
        queued = bool(run.queued)
        # A resumed run has already spent money. Start the meter from there, or
        # a restart would show the spend falling to zero and let a spend limit
        # be spent twice.
        already_spent = float(run.spend_usd or 0)
        already_in, already_out = int(run.tokens_in or 0), int(run.tokens_out or 0)

    limits = RunLimits(
        max_records=config.get("max_records"),
        max_trainees=config.get("max_trainees"),
        max_emails=config.get("max_emails"),
        max_spend_usd=config.get("max_spend_usd"),
        spend_usd=already_spent,
        tokens_in=already_in,
        tokens_out=already_out,
    )
    # A queued run is one school at a time, in the order they were listed.
    # Several schools crawled side by side split the one model budget.
    concurrency = 1 if queued else int(config.get("concurrency", settings.default_concurrency))
    orchestrator = RunOrchestrator(
        run_id,
        concurrency=concurrency,
        step_budget=int(config.get("step_budget", settings.default_step_budget)),
        limits=limits,
        use_browser=use_browser,
        crawl_strategy=config.get("crawl_strategy"),
        modes=config.get("modes"),
    )
    register(orchestrator)

    async def _run() -> None:
        try:
            await orchestrator.start()
        except MemoryCeilingExceeded as exc:
            log.error("run %s refused: %s", run_id, exc)
            await _mark_failed(run_id, str(exc), orchestrator.limits)
        except Exception as exc:
            log.exception("run %s crashed", run_id)
            await _mark_failed(run_id, f"{type(exc).__name__}: {exc}"[:500], orchestrator.limits)
        finally:
            unregister(run_id)
            # Only now is the model budget free, so this is where the next
            # queued run may start.
            from .scheduler import on_run_finished

            await on_run_finished(run_id)

    task = asyncio.create_task(_run())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return orchestrator


async def _mark_failed(run_id: str, message: str, limits: RunLimits | None = None) -> None:
    """End a run that crashed, keeping what it had already spent and reporting why."""
    values: dict = {
        "status": RunStatus.FAILED,
        "error_message": message,
        "finished_at": datetime.now(UTC),
    }
    if limits is not None:
        values.update(
            tokens_in=limits.tokens_in, tokens_out=limits.tokens_out,
            spend_usd=limits.spend_usd,
        )
    async with get_sessionmaker()() as session:
        await session.execute(update(Run).where(Run.id == run_id).values(**values))
        await session.commit()
    # The stream ends on a terminal event; without one a crashed run left every
    # open monitor waiting for a message that never came.
    await EventEmitter(run_id).emit(EventType.RUN_FAILED, status="failed", error=message)


async def cancel_run(session: AsyncSession, run_id: str) -> Run:
    """Stop taking new work immediately and leave the data consistent."""
    from .queue import cancel_pending

    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError(f"no run {run_id}")

    await cancel_pending(session, run_id)
    orchestrator = None
    from .pool import get_active

    orchestrator = get_active(run_id)
    if orchestrator is not None:
        await orchestrator.cancel()
    else:
        run.status = RunStatus.CANCELLED
        run.finished_at = datetime.now(UTC)
        await session.commit()
    return run


async def retry_site(session: AsyncSession, run_id: str, site_id: str) -> SiteRun:
    """Return one failed site to the queue to be crawled again."""
    site_run = await session.scalar(
        select(SiteRun).where(SiteRun.run_id == run_id, SiteRun.site_id == site_id)
    )
    if site_run is None:
        raise LookupError("site is not part of this run")
    if site_run.status not in TERMINAL_SITE_RUN_STATUSES:
        raise ValueError(f"site is {site_run.status}; only finished sites can be retried")

    site_run.status = SiteRunStatus.PENDING
    site_run.error_code = None
    site_run.error_message = None
    site_run.steps_taken = 0
    site_run.finished_at = None
    site_run.agent_id = None
    await session.commit()
    return site_run
