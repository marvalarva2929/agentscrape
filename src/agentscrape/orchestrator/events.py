"""Progress events.

Two consumers: the SSE stream and the database counters. Individual events are
lost across a reconnect, so the periodic heartbeat carries authoritative
aggregates and the frontend treats those as the source of truth for counters.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

log = logging.getLogger("agentscrape.events")

# Bounded so a slow or dead SSE client cannot grow memory without limit.
SUBSCRIBER_QUEUE_SIZE = 256

# What a monitor opened mid-crawl is shown: the last events of each run, and
# who is working on what. Bounded per run and in the number of runs kept.
HISTORY_PER_RUN = 60
HISTORY_RUNS = 20
# Fields that say what an agent is doing, copied onto its roster entry.
AGENT_FIELDS = (
    "site_id", "site_run_id", "domain", "url", "action", "message", "title",
    "page_type", "program", "stage", "steps_taken", "step_budget", "records_found",
)


class RunStage(StrEnum):
    """Coarse phase, for the monitor's stage indicator.

    Deliberately not one chip per pipeline node: the user wants to know roughly
    where a crawl is, not which graph node is executing.
    """

    QUEUED = "queued"
    DISCOVERING = "discovering"   # validating, skip-checking, finding candidates
    DIRECTORY = "directory"       # working the candidate list
    FINALIZING = "finalizing"     # reconciling and closing out
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    AGENT_SPAWNED = "agent_spawned"
    AGENT_RETIRED = "agent_retired"
    SITE_STARTED = "site_started"
    SITE_STEP = "site_step"
    SITE_SKIPPED = "site_skipped"
    KNOWN_PATH_HIT = "known_path_hit"
    SITE_COMPLETED = "site_completed"
    SITE_FAILED = "site_failed"
    SITE_REJECTED = "site_rejected"
    RUN_PROGRESS = "run_progress"
    RUN_COMPLETED = "run_completed"
    RUN_STOPPED_AT_LIMIT = "run_stopped_at_limit"
    RUN_CANCELLED = "run_cancelled"
    RUN_FAILED = "run_failed"
    HEARTBEAT = "heartbeat"


# The stream ends on these and no earlier: a limit tripping while another school
# is still being read is a `run_progress`, not an end.
TERMINAL_EVENTS = frozenset(
    {
        EventType.RUN_COMPLETED,
        EventType.RUN_STOPPED_AT_LIMIT,
        EventType.RUN_CANCELLED,
        EventType.RUN_FAILED,
    }
)
# Periodic or superseded: worth streaming, not worth replaying to a late client.
NOT_REPLAYED = frozenset({EventType.HEARTBEAT, EventType.RUN_PROGRESS})


@dataclass
class Event:
    type: EventType
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Position in the bus's order, so a client that was shown the history can
    # skip the same events when they arrive on its live queue.
    seq: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "run_id": self.run_id,
            "at": self.at.isoformat(),
            "seq": self.seq,
            **self.data,
        }


class EventBus:
    """In-process fan-out, one queue per subscriber, scoped by run."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
        self._seq = 0
        self._history: OrderedDict[str, deque[Event]] = OrderedDict()
        self._agents: dict[str, dict[str, dict[str, Any]]] = {}

    def _record(self, event: Event) -> None:
        """Keep what a late subscriber needs: recent events and the agent roster."""
        if event.type not in NOT_REPLAYED:
            history = self._history.get(event.run_id)
            if history is None:
                history = self._history[event.run_id] = deque(maxlen=HISTORY_PER_RUN)
                while len(self._history) > HISTORY_RUNS:
                    dropped, _ = self._history.popitem(last=False)
                    self._agents.pop(dropped, None)
            history.append(event)

        agent_id = event.data.get("agent_id")
        if event.type in TERMINAL_EVENTS:
            self._agents.pop(event.run_id, None)
        elif agent_id:
            agents = self._agents.setdefault(event.run_id, {})
            if event.type == EventType.AGENT_RETIRED:
                agents.pop(agent_id, None)
            else:
                info = agents.setdefault(agent_id, {"agent_id": agent_id})
                for key in AGENT_FIELDS:
                    if event.data.get(key) is not None:
                        info[key] = event.data[key]
                info["at"] = event.at.isoformat()

    def snapshot(self, run_id: str) -> tuple[list[Event], list[dict[str, Any]], int]:
        """Recent events, the agents working now, and the last sequence number."""
        return (
            list(self._history.get(run_id, ())),
            [dict(info) for info in self._agents.get(run_id, {}).values()],
            self._seq,
        )

    async def publish(self, event: Event) -> None:
        self._seq += 1
        event.seq = self._seq
        self._record(event)
        for queue in list(self._subscribers.get(event.run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop rather than block the pipeline. The heartbeat is
                # authoritative, so a slow client resyncs on the next one.
                log.debug("dropping event for a slow subscriber on run %s", event.run_id)

    def subscribe(self, run_id: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.get(run_id, set()).discard(queue)
        if not self._subscribers.get(run_id):
            self._subscribers.pop(run_id, None)

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscribers.get(run_id, ()))


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


class EventEmitter:
    """Convenience wrapper bound to one run (and optionally one site)."""

    def __init__(self, run_id: str, bus: EventBus | None = None) -> None:
        self.run_id = run_id
        self.bus = bus or get_event_bus()

    async def emit(self, event_type: EventType, **data: Any) -> None:
        await self.bus.publish(Event(type=event_type, run_id=self.run_id, data=data))


class NullEmitter(EventEmitter):
    """Used by the single-site CLI, where there is no stream to feed."""

    def __init__(self) -> None:
        super().__init__(run_id="cli")

    async def emit(self, event_type: EventType, **data: Any) -> None:
        log.debug("event %s %s", event_type, data)
