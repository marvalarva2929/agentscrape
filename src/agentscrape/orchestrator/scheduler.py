"""One run at a time: the queue that stops schools sharing the model budget.

`LLM_CONCURRENCY` is a single process-wide gate (`llm.provider._gate`), so N
runs at once get roughly 1/N of the model throughput each while every one of
them still pays its own discovery and link-ranking startup in full. Measured on
this code: one school alone for 30 minutes found 883 residents for $0.89, while
three side by side for the same 30 minutes spent $1.30 between them and
surfaced 22 — the money went into three startups instead of one harvest.

Every run made through the API is queued: it waits for the one running run to
finish, and the next one starts the moment it does. "Something is running" is
read from the database, not from this process's memory, so the answer survives
a restart and is the same in every process. Three layers keep it to one:

* an in-process lock, so two requests in one process do not race;
* `pg_advisory_xact_lock`, so two processes do not race;
* a partial unique index on `runs`, so that even a bug cannot start a second
  queued run — the insert of the second `running` row fails.

A run whose process died is left `running` with a heartbeat that stops moving.
`claim_next` puts such a run back at the front of the queue once the heartbeat
is older than `RUN_STALE_SECONDS`, and `supervise` calls it every few seconds,
so a restart cannot leave the queue stuck behind a run nobody is working on.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.enums import RunStatus, SiteRunStatus
from ..db.models import Run, Site, SiteRun
from ..db.session import get_sessionmaker
from ..domain.schemas import QueueEntryOut, QueueOut, QueueSiteOut
from .pool import active_run_ids

log = logging.getLogger("agentscrape.scheduler")

# The orchestrator beats every couple of seconds, so a run silent this long has
# no process working on it. Long enough to ride out a slow database, short
# enough that a restart does not leave the queue idle for minutes.
RUN_STALE_SECONDS = 120
SUPERVISE_SECONDS = 15.0

# Any constant: every claim and reorder takes this lock, in every process.
QUEUE_LOCK_KEY = 0x51_45_55_45_01

# Runs still waiting or in flight.
_LIVE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING)

# Serialises the gap between "nothing is active" and "this run is registered",
# during which a second caller in this process would otherwise start a second run.
_lock = asyncio.Lock()
_lock_loop: asyncio.AbstractEventLoop | None = None


def _gate() -> asyncio.Lock:
    """Bind the lock to the running loop, as the model gate does."""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


async def _take_queue_lock(session: AsyncSession) -> None:
    """Held until the transaction ends. Claims and reorders queue behind it."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": QUEUE_LOCK_KEY}
    )


def _order():
    return (Run.queue_rank, Run.created_at, Run.id)


def _is_fresh(beat: datetime | None, now: datetime) -> bool:
    return beat is not None and beat >= now - timedelta(seconds=RUN_STALE_SECONDS)


def _newest(*beats: datetime | None) -> datetime | None:
    seen = [b for b in beats if b is not None]
    return max(seen) if seen else None


async def next_rank(session: AsyncSession) -> int:
    """The rank that puts a new run behind everything already waiting."""
    top = await session.scalar(
        select(func.max(Run.queue_rank)).where(
            Run.queued.is_(True), Run.status.in_(_LIVE_STATUSES)
        )
    )
    return int(top or 0) + 1


async def _waiting_ids(session: AsyncSession) -> list[str]:
    """Queued runs still waiting for their turn, in the order they will start."""
    rows = await session.execute(
        select(Run.id)
        .where(Run.queued.is_(True), Run.status == RunStatus.PENDING)
        .order_by(*_order())
    )
    return list(rows.scalars())


async def queue_position(session: AsyncSession, run_id: str) -> int | None:
    """1 for the run that goes next; None if it is not waiting in the queue."""
    waiting = await _waiting_ids(session)
    return waiting.index(run_id) + 1 if run_id in waiting else None


async def _claim_next() -> str | None:
    """Mark the next waiting run `running` if nothing else is. Returns its id.

    One transaction under the queue lock. Anything already running blocks the
    claim, unless its heartbeat stopped — then its process is gone and it goes
    back to waiting, at the front, so it resumes before anything newer starts.
    """
    async with get_sessionmaker()() as session:
        try:
            async with session.begin():
                await _take_queue_lock(session)
                if active_run_ids():
                    return None

                now = datetime.now(UTC)
                running = list(
                    (
                        await session.execute(
                            select(Run).where(
                                Run.queued.is_(True), Run.status == RunStatus.RUNNING
                            )
                        )
                    ).scalars()
                )
                if running:
                    beats = dict(
                        (
                            await session.execute(
                                select(SiteRun.run_id, func.max(SiteRun.heartbeat_at))
                                .where(SiteRun.run_id.in_([r.id for r in running]))
                                .group_by(SiteRun.run_id)
                            )
                        ).all()
                    )
                    for run in running:
                        if _is_fresh(_newest(run.heartbeat_at, beats.get(run.id)), now):
                            return None  # a live process owns it
                        log.warning(
                            "queue: run %s was left running with no heartbeat; "
                            "putting it back in line", run.id,
                        )
                        run.status = RunStatus.PENDING
                        run.heartbeat_at = None
                    await session.flush()

                nxt = (
                    await session.execute(
                        select(Run)
                        .where(Run.queued.is_(True), Run.status == RunStatus.PENDING)
                        .order_by(*_order())
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if nxt is None:
                    return None
                nxt.status = RunStatus.RUNNING
                nxt.started_at = nxt.started_at or now
                nxt.heartbeat_at = now
                await session.flush()
                return nxt.id
        except IntegrityError:
            # The unique index refused a second running run: someone else won.
            log.warning("queue: another run is already running; not starting one")
            return None


async def _unclaim(run_id: str) -> None:
    """Put a run that could not be launched back at the front of the queue."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.RUNNING)
            .values(status=RunStatus.PENDING, heartbeat_at=None)
        )
        await session.commit()


async def start_next_if_idle() -> str | None:
    """Launch the next waiting run, but only when nothing is running.

    Safe to call whenever the picture might have changed — on creation, when a
    run finishes, on startup and on a timer. It is a no-op if a run is going or
    the queue is empty.
    """
    async with _gate():
        run_id = await _claim_next()
        if run_id is None:
            return None

        # Imported here: service owns launching and already imports this module.
        from .service import launch_run

        log.info("queue: starting run %s", run_id)
        try:
            await launch_run(run_id)
        except Exception:
            # Claimed but never started: without this the row would read
            # `running` and hold the queue until its heartbeat expired.
            log.exception("queue: could not launch run %s; returning it to the queue", run_id)
            await _unclaim(run_id)
            return None
        return run_id


async def move_run(session: AsyncSession, run_id: str, direction: str) -> list[str]:
    """Reorder a waiting run. `direction` is `up`, `down` or `top`.

    Returns the waiting order afterwards. Only a run that has not started can
    move; the one already running keeps its place at the front.
    """
    if direction not in ("up", "down", "top"):
        raise ValueError("direction must be up, down or top")

    await _take_queue_lock(session)
    order = await _waiting_ids(session)
    if run_id not in order:
        raise LookupError("only a waiting run can be moved")

    index = order.index(run_id)
    target = {"up": max(index - 1, 0), "down": min(index + 1, len(order) - 1), "top": 0}[direction]
    order.insert(target, order.pop(index))

    for rank, waiting_id in enumerate(order, start=1):
        await session.execute(update(Run).where(Run.id == waiting_id).values(queue_rank=rank))
    await session.commit()
    return order


async def _sites_by_run(
    session: AsyncSession, run_ids: list[str]
) -> dict[str, list[QueueSiteOut]]:
    """Every school in the given runs, in the order they are crawled."""
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(SiteRun, Site.root_domain, Site.hospital_name)
            .join(Site, Site.id == SiteRun.site_id)
            .where(SiteRun.run_id.in_(run_ids))
            .order_by(SiteRun.run_id, SiteRun.position, SiteRun.created_at, SiteRun.id)
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
                select(Run).where(Run.status.in_(_LIVE_STATUSES)).order_by(*_order())
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
    now = datetime.now(UTC)
    active = set(active_run_ids())

    def in_flight(run: Run) -> bool:
        """Beating recently, or claimed by an orchestrator in this process."""
        if run.id in active:
            return True
        return _is_fresh(_newest(run.heartbeat_at, heartbeats.get(run.id)), now)

    def entry(run: Run, position: int | None) -> QueueEntryOut:
        own = sites.get(run.id, [])
        return QueueEntryOut(
            run_id=run.id,
            label=run.label,
            status=run.status,
            queued=run.queued,
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
            (view.running if in_flight(run) else view.stalled).append(entry(run, None))
        elif run.queued:
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


async def supervise(interval: float = SUPERVISE_SECONDS) -> None:
    """Keep the queue moving: start what is waiting, reclaim what was abandoned.

    A run finishing and a run being created both call `start_next_if_idle`
    directly. This covers the cases nothing calls it for: the process was
    restarted while a run was going, so the run is abandoned and only its
    expiring heartbeat says so.
    """
    while True:
        try:
            await start_next_if_idle()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("queue: supervisor could not start the next run")
        await asyncio.sleep(interval)


__all__ = [
    "RUN_STALE_SECONDS",
    "move_run",
    "next_rank",
    "on_run_finished",
    "queue_position",
    "queue_view",
    "start_next_if_idle",
    "supervise",
]
