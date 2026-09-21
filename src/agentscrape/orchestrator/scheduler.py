"""One run at a time: the queue that stops schools sharing the model budget.

`LLM_CONCURRENCY` is a single process-wide gate (`llm.provider._gate`), so N
runs at once get roughly 1/N of the model throughput each while every one of
them still pays its own discovery and link-ranking startup in full. Measured on
this code: one school alone for 30 minutes found 883 residents for $0.89, while
three side by side for the same 30 minutes spent $1.30 between them and
surfaced 22 — the money went into three startups instead of one harvest.

A run created with `config.queued` therefore waits for the process to go idle
instead of starting beside its neighbours, and the next one starts the moment
the previous finishes. The queue is in-process, like the `pool` registry it
reads: it orders the runs that share this interpreter's model gate, which is
exactly the resource being rationed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.enums import RunStatus, SiteRunStatus
from ..db.models import Run, Site, SiteRun
from ..db.session import get_sessionmaker
from ..domain.schemas import QueueEntryOut, QueueOut, QueueSiteOut
from .pool import active_run_ids
from .queue import STALE_CLAIM_MINUTES

log = logging.getLogger("agentscrape.scheduler")

# Serialises the gap between "nothing is active" and "this run is registered",
# during which a second caller would otherwise start a second run.
_lock = asyncio.Lock()
_lock_loop: asyncio.AbstractEventLoop | None = None

# Runs still waiting or in flight. A queued run that is RUNNING here but absent
# from the in-process registry was left behind by a killed process.
_LIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING)


def _gate() -> asyncio.Lock:
    """Bind the lock to the running loop, as the model gate does."""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def is_queued(config: dict | None) -> bool:
    return bool((config or {}).get("queued"))


async def _waiting(session: AsyncSession) -> list[tuple[str, dict]]:
    """Queued runs that have not finished, oldest first.

    Filtered in Python rather than with a JSON operator: the set of unfinished
    runs is tiny, and this stays portable across the JSON and JSONB variants
    the `config` column is declared with.
    """
    rows = (
        await session.execute(
            select(Run.id, Run.config)
            .where(Run.status.in_(_LIVE_STATUSES))
            .order_by(Run.created_at)
        )
    ).all()
    return [(run_id, config or {}) for run_id, config in rows if is_queued(config)]


async def queue_position(session: AsyncSession, run_id: str) -> int | None:
    """1 for the run that goes next; None if it is not waiting in the queue."""
    waiting = await _waiting(session)
    for position, (candidate, _) in enumerate(waiting, start=1):
        if candidate == run_id:
            return position
    return None


async def start_next_if_idle() -> str | None:
    """Launch the oldest queued run, but only when nothing else is running.

    Safe to call whenever the picture might have changed — on creation, when a
    run finishes, and on startup. It is a no-op if the process is busy or the
    queue is empty.
    """
    async with _gate():
        if active_run_ids():
            return None
        async with get_sessionmaker()() as session:
            waiting = await _waiting(session)
        if not waiting:
            return None

        run_id, _ = waiting[0]
        # Imported here: service owns launching and already imports this module.
        from .service import launch_run

        log.info("queue: starting run %s (%d waiting)", run_id, len(waiting))
        await launch_run(run_id)
        return run_id


async def _sites_by_run(
    session: AsyncSession, run_ids: list[str]
) -> dict[str, list[QueueSiteOut]]:
    """Every school in the given runs, in a stable display order.

    `created_at` defaults to `now()`, which in Postgres is the transaction
    start, so every school created with its run shares one timestamp; `id`
    breaks the tie so repeated calls agree. Workers claim by `created_at`
    alone, so this is a stable order to read, not a promise of crawl order.
    """
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(SiteRun, Site.root_domain, Site.hospital_name)
            .join(Site, Site.id == SiteRun.site_id)
            .where(SiteRun.run_id.in_(run_ids))
            .order_by(SiteRun.run_id, SiteRun.created_at, SiteRun.id)
        )
    ).all()

    out: dict[str, list[QueueSiteOut]] = {run_id: [] for run_id in run_ids}
    for site_run, domain, hospital in rows:
        out[site_run.run_id].append(
            QueueSiteOut(
                site_id=site_run.site_id,
                domain=domain,
                hospital=hospital,
                status=site_run.status,
                records_found=site_run.records_found or 0,
                steps_taken=site_run.steps_taken or 0,
                step_budget=site_run.step_budget or 0,
            )
        )
    return out


async def queue_view(session: AsyncSession) -> QueueOut:
    """What holds the model budget now, and what is waiting for it, in order.

    Stalled runs — claiming to run with no recent heartbeat, or created and
    never launched — are reported rather than hidden, because one of those is
    exactly what someone looking at a stuck queue needs to see.
    """
    runs = list(
        (
            await session.execute(
                select(Run)
                .where(Run.status.in_(_LIVE_STATUSES))
                .order_by(Run.created_at)
            )
        ).scalars()
    )
    if not runs:
        return QueueOut()

    run_ids = [run.id for run in runs]
    sites = await _sites_by_run(session, run_ids)
    # Classified from the database, not from the in-process registry, so the
    # view is the same whether it is read inside the API or from the CLI in a
    # separate process, where nothing would ever look like it was running.
    heartbeats = dict(
        (
            await session.execute(
                select(SiteRun.run_id, func.max(SiteRun.heartbeat_at))
                .where(SiteRun.run_id.in_(run_ids))
                .group_by(SiteRun.run_id)
            )
        ).all()
    )
    fresh_after = datetime.now(UTC) - timedelta(minutes=STALE_CLAIM_MINUTES)
    active = set(active_run_ids())

    def in_flight(run: Run) -> bool:
        """Beating recently, or claimed by an orchestrator in this process."""
        if run.id in active:
            return True
        beat = heartbeats.get(run.id)
        return beat is not None and beat >= fresh_after

    def entry(run: Run, position: int | None) -> QueueEntryOut:
        own = sites.get(run.id, [])
        return QueueEntryOut(
            run_id=run.id,
            label=run.label,
            status=run.status,
            queued=is_queued(run.config),
            running=in_flight(run),
            position=position,
            sites_total=run.sites_total or 0,
            sites_completed=run.sites_completed or 0,
            sites_pending=sum(1 for s in own if s.status == SiteRunStatus.PENDING),
            records_found=run.records_found or 0,
            spend_usd=float(run.spend_usd or 0),
            created_at=run.created_at,
            started_at=run.started_at,
            sites=own,
        )

    view = QueueOut()
    position = 0
    for run in runs:
        if run.status == RunStatus.RUNNING or run.id in active:
            (view.running if in_flight(run) else view.stalled).append(
                entry(run, None)
            )
        elif is_queued(run.config):
            position += 1
            view.waiting.append(entry(run, position))
        else:
            # Created but never launched, and not queued either.
            view.stalled.append(entry(run, None))
    return view


async def on_run_finished(run_id: str) -> None:
    """Hand the model budget to whichever run is next in line."""
    try:
        await start_next_if_idle()
    except Exception:
        # A failure here must not bury the error that ended the finished run.
        log.exception("queue: could not start the run after %s", run_id)


__all__ = [
    "is_queued",
    "on_run_finished",
    "queue_position",
    "queue_view",
    "start_next_if_idle",
]
