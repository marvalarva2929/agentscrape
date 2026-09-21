"""Server-sent events for the live run monitor.

Two guarantees the frontend relies on:
  * a periodic heartbeat carries authoritative aggregate counters, because
    individual events are lost across a reconnect;
  * `GET /runs/{id}/sites` returns the same state as a snapshot, so a
    reconnecting client resyncs in one call instead of replaying.

A stream can end without a completion event because the server was stopped.
That is expected, and the client should fall back to the snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from ..db.enums import TERMINAL_RUN_STATUSES
from ..db.models import Run
from ..db.session import get_sessionmaker
from ..orchestrator.events import TERMINAL_EVENTS, Event, EventType, get_event_bus
from ..orchestrator.pool import get_active

log = logging.getLogger("agentscrape.sse")

# Keeps proxies from closing an idle connection while a slow site is in flight.
IDLE_PING_SECONDS = 15.0


def format_event(event: Event) -> str:
    payload = json.dumps(event.to_json(), default=str)
    return f"event: {event.type}\ndata: {payload}\n\n"


def _live_figures(run: Run) -> dict[str, object]:
    """The run's counters, from the running orchestrator when there is one.

    The database takes spend and people every couple of seconds, so it is close,
    but a client that connects should not be a heartbeat behind the meter.
    """
    orchestrator = get_active(run.id)
    if orchestrator is not None:
        return orchestrator.limits.snapshot()
    return {
        "records_collected": run.records_found,
        "spend_usd": float(run.spend_usd or 0),
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "stop_reason": run.stop_reason,
    }


async def event_stream(run_id: str) -> AsyncIterator[str]:
    """Yield SSE frames for one run until it reaches a terminal state."""
    bus = get_event_bus()
    queue = bus.subscribe(run_id)
    sessionmaker = get_sessionmaker()

    try:
        # Subscribed before the snapshot is taken, so nothing is missed between
        # the two; `seq` drops the events that turn up in both.
        history, agents, replayed_to = bus.snapshot(run_id)

        # Open with a snapshot so a client that connects late is never blank:
        # the counters, who is working on what, and the recent activity.
        async with sessionmaker() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                yield format_event(
                    Event(
                        type=EventType.RUN_PROGRESS,
                        run_id=run_id,
                        data={
                            "status": run.status,
                            "sites_total": run.sites_total,
                            "sites_completed": run.sites_completed,
                            "sites_skipped": run.sites_skipped,
                            "sites_failed": run.sites_failed,
                            "sites_rejected": run.sites_rejected,
                            "records_found": run.records_found,
                            **_live_figures(run),
                            "active_agents": len(agents),
                            "agents": agents,
                            "snapshot": True,
                        },
                    )
                )
                for event in history:
                    yield format_event(event)
                if run.status in TERMINAL_RUN_STATUSES:
                    return

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=IDLE_PING_SECONDS)
            except TimeoutError:
                # Comment frame: keeps the connection alive, ignored by EventSource.
                yield ": ping\n\n"
                async with sessionmaker() as session:
                    run = await session.get(Run, run_id)
                if run is not None and run.status in TERMINAL_RUN_STATUSES:
                    return
                continue

            if event.seq and event.seq <= replayed_to:
                continue
            yield format_event(event)
            if event.type in TERMINAL_EVENTS:
                return

    except asyncio.CancelledError:
        log.debug("SSE client disconnected from run %s", run_id)
        raise
    finally:
        bus.unsubscribe(run_id, queue)
