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
from ..db.repositories.records import stored_identity_keys
from ..db.repositories.sites import upsert_site
from ..db.session import get_sessionmaker
from ..domain.schemas import CsvRowPreview, RunCreate, ValidatePreview
from ..urls import canonicalize, host_of
from ..validation.institution import classify_domain
from .limits import MemoryCeilingExceeded, RunLimits, check_memory_ceiling
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
    session: AsyncSession, entries: list[tuple[int, str]], *, skip_threshold: float
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
        identities = await stored_identity_keys(session, site.id)
        # A site can only be skipped if it has both stored records and a probe
        # path to check them against.
        predicted_skip = bool(identities) and path_count > 0

        rows.append(
            CsvRowPreview(
                row=row_number, input=raw, url=url, valid=True, site_id=site.id,
                known_site=True, last_scraped_at=site.last_scraped_at,
                known_path_count=path_count, previous_record_count=record_count,
                predicted_skip=predicted_skip,
                validation_status=site.validation_status,
                validation_reason=site.validation_reason,
            )
        )

    return ValidatePreview(
        total_rows=len(rows),
        valid_rows=sum(1 for r in rows if r.valid),
        invalid_rows=sum(1 for r in rows if not r.valid),
        known_sites=sum(1 for r in rows if r.known_site),
        predicted_skips=sum(1 for r in rows if r.predicted_skip),
        rejected_sites=sum(
            1 for r in rows if r.validation_status == str(ValidationStatus.K12_REJECTED)
        ),
        rows=rows,
    )


async def create_run(session: AsyncSession, body: RunCreate) -> Run:
    """Create the Run and one SiteRun per valid site. Does not start it."""
    config = body.config
    # Reject before anything is written, so the client gets a clean error.
    check_memory_ceiling(config.concurrency)

    run = Run(
        status=RunStatus.PENDING,
        label=config.label,
        config=config.model_dump(),
        sites_total=0,
    )
    session.add(run)
    await session.flush()

    force_set = {s.strip().lower() for s in config.force_rescan_sites}
    created = 0
    duplicates = 0
    # A run has one SiteRun per site. Several inputs can resolve to the same
    # institution ("uni.edu", "www.uni.edu/", "https://uni.edu/index.html"), and
    # without collapsing them the unique constraint would fail the whole run.
    seen_site_ids: set[str] = set()

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

        forced = (
            config.force_rescan
            or site.id.lower() in force_set
            or site.root_domain.lower() in force_set
        )
        session.add(
            SiteRun(
                run_id=run.id,
                site_id=site.id,
                status=SiteRunStatus.PENDING,
                force_rescan=forced,
                step_budget=config.step_budget,
            )
        )
        created += 1

    run.sites_total = created
    await session.commit()
    log.info(
        "created run %s with %d sites (%d duplicate inputs collapsed)",
        run.id, created, duplicates,
    )
    return run


async def launch_run(run_id: str, *, use_browser: bool = True) -> RunOrchestrator:
    """Start the orchestrator for a run as a background task."""
    async with get_sessionmaker()() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise ValueError(f"no run {run_id}")
        config = dict(run.config or {})

    limits = RunLimits(
        max_records=config.get("max_records"),
        max_spend_usd=config.get("max_spend_usd"),
    )
    orchestrator = RunOrchestrator(
        run_id,
        concurrency=int(config.get("concurrency", settings.default_concurrency)),
        skip_threshold=float(config.get("skip_threshold", settings.default_skip_threshold)),
        step_budget=int(config.get("step_budget", settings.default_step_budget)),
        limits=limits,
        use_browser=use_browser,
    )
    register(orchestrator)

    async def _run() -> None:
        try:
            await orchestrator.start()
        except MemoryCeilingExceeded as exc:
            log.error("run %s refused: %s", run_id, exc)
            await _mark_failed(run_id, str(exc))
        except Exception as exc:
            log.exception("run %s crashed", run_id)
            await _mark_failed(run_id, f"{type(exc).__name__}: {exc}"[:500])
        finally:
            unregister(run_id)

    task = asyncio.create_task(_run())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return orchestrator


async def _mark_failed(run_id: str, message: str) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=RunStatus.FAILED, error_message=message,
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()


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
    """Return one failed or skipped site to the queue with a forced rescan."""
    site_run = await session.scalar(
        select(SiteRun).where(SiteRun.run_id == run_id, SiteRun.site_id == site_id)
    )
    if site_run is None:
        raise LookupError("site is not part of this run")
    if site_run.status not in TERMINAL_SITE_RUN_STATUSES:
        raise ValueError(f"site is {site_run.status}; only finished sites can be retried")

    site_run.status = SiteRunStatus.PENDING
    site_run.force_rescan = True          # a retry always means "look again"
    site_run.error_code = None
    site_run.error_message = None
    site_run.skip_reason = None
    site_run.similarity_score = None
    site_run.steps_taken = 0
    site_run.finished_at = None
    site_run.agent_id = None
    await session.commit()
    return site_run
