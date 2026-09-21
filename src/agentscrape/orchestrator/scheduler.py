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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.enums import RunStatus
from ..db.models import Run
from ..db.session import get_sessionmaker
from .pool import active_run_ids

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
    "start_next_if_idle",
]
