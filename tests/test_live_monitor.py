"""What the run monitor is told, and whether it can be told again.

The client saw spend that did not move and agent information that never
updated. These cover the two halves: the numbers are saved to the database
while the crawl goes on, and a monitor that opens late, or reconnects, is shown
the current state instead of a blank page.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select, update

from agentscrape.api.sse import event_stream
from agentscrape.db.enums import RunStatus, SiteRunStatus
from agentscrape.db.models import Run, SiteRun
from agentscrape.orchestrator import events, pool, service
from agentscrape.orchestrator.events import EventBus, EventEmitter, EventType
from agentscrape.orchestrator.limits import RunLimits
from agentscrape.orchestrator.pool import RunOrchestrator

from .test_run_queue import _seed_run, _seed_run_with_sites


@pytest.fixture(autouse=True)
def fresh_bus(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(events, "_bus", bus)
    return bus


def _parse(frame: str) -> dict | None:
    if frame.startswith(":"):
        return None
    body = frame.split("data: ", 1)[1]
    return json.loads(body)


async def _next(stream, timeout: float = 3.0) -> dict:
    while True:
        frame = await asyncio.wait_for(stream.__anext__(), timeout)
        parsed = _parse(frame)
        if parsed is not None:
            return parsed


async def _running_run(session) -> str:
    run_id = await _seed_run(session, queued=True, label="live")
    await session.execute(update(Run).where(Run.id == run_id).values(status=RunStatus.RUNNING))
    await session.commit()
    return run_id


class TestLateViewers:
    async def test_the_first_frame_says_who_is_working_on_what(self, session):
        run_id = await _running_run(session)
        emitter = EventEmitter(run_id)
        await emitter.emit(EventType.AGENT_SPAWNED, agent_id="agent-0")
        await emitter.emit(
            EventType.SITE_STEP, agent_id="agent-0", domain="med.example.edu",
            url="https://med.example.edu/residents", action="fetch:http",
            message="Read the residents page", steps_taken=4, step_budget=40,
        )

        stream = event_stream(run_id)
        first = await _next(stream)

        assert first["type"] == "run_progress" and first["snapshot"] is True
        assert first["active_agents"] == 1
        agent = first["agents"][0]
        assert agent["agent_id"] == "agent-0"
        assert agent["url"] == "https://med.example.edu/residents"
        assert (agent["steps_taken"], agent["step_budget"]) == (4, 40)
        assert agent["message"] == "Read the residents page"
        await stream.aclose()

    async def test_recent_activity_is_replayed_once(self, session, fresh_bus):
        run_id = await _running_run(session)
        emitter = EventEmitter(run_id)
        for n in range(3):
            await emitter.emit(EventType.SITE_STEP, agent_id="agent-0", message=f"page {n}")

        stream = event_stream(run_id)
        await _next(stream)  # the snapshot
        replayed = [await _next(stream) for _ in range(3)]
        assert [e["message"] for e in replayed] == ["page 0", "page 1", "page 2"]

        # A new event arrives once, and the replayed ones are not repeated.
        await emitter.emit(EventType.SITE_STEP, agent_id="agent-0", message="page 3")
        live = await _next(stream)
        assert live["message"] == "page 3"
        await stream.aclose()

    async def test_a_retired_agent_leaves_the_roster(self, session):
        run_id = await _running_run(session)
        emitter = EventEmitter(run_id)
        await emitter.emit(EventType.AGENT_SPAWNED, agent_id="agent-0")
        await emitter.emit(EventType.AGENT_RETIRED, agent_id="agent-0")

        stream = event_stream(run_id)
        assert (await _next(stream))["agents"] == []
        await stream.aclose()

    async def test_the_snapshot_carries_the_live_meter(self, session):
        run_id = await _running_run(session)
        await session.execute(
            update(Run).where(Run.id == run_id).values(spend_usd=1.5, tokens_in=900, tokens_out=90)
        )
        await session.commit()

        stream = event_stream(run_id)
        first = await _next(stream)
        assert first["spend_usd"] == pytest.approx(1.5)
        assert (first["tokens_in"], first["tokens_out"]) == (900, 90)
        await stream.aclose()


class TestTheStreamEndsWhenTheRunDoes:
    async def test_a_limit_being_reached_is_progress_not_the_end(self, session):
        """Other schools, or a directory search, may still be going."""
        run_id = await _running_run(session)
        emitter = EventEmitter(run_id)
        stream = event_stream(run_id)
        await _next(stream)

        await emitter.emit(EventType.RUN_PROGRESS, stop_reason="max_records", spend_usd=1.0)
        assert (await _next(stream))["stop_reason"] == "max_records"

        await emitter.emit(EventType.HEARTBEAT, spend_usd=1.1)
        assert (await _next(stream))["spend_usd"] == 1.1  # still open
        await stream.aclose()

    @pytest.mark.parametrize(
        "terminal",
        [
            EventType.RUN_COMPLETED,
            EventType.RUN_STOPPED_AT_LIMIT,
            EventType.RUN_CANCELLED,
            EventType.RUN_FAILED,
        ],
    )
    async def test_a_terminal_event_ends_it(self, session, terminal):
        run_id = await _running_run(session)
        stream = event_stream(run_id)
        await _next(stream)

        await EventEmitter(run_id).emit(terminal, status="done")
        assert (await _next(stream))["type"] == str(terminal)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(stream.__anext__(), 2)


class TestHeartbeat:
    async def test_it_saves_spend_and_says_the_run_is_alive(self, session):
        run_id = await _seed_run_with_sites(session, queued=True, label="beat", domains=["a.edu"])
        limits = RunLimits(spend_usd=0.4)
        orchestrator = RunOrchestrator(
            run_id, concurrency=1, skip_threshold=0.9, step_budget=5, limits=limits,
            use_browser=False,
        )
        await limits.add_usage(10, 5, 0.1)

        bus = events.get_event_bus()
        queue = bus.subscribe(run_id)
        await orchestrator._beat()

        session.expire_all()
        run = await session.get(Run, run_id)
        assert float(run.spend_usd) == pytest.approx(0.5)
        assert run.heartbeat_at is not None
        event = await asyncio.wait_for(queue.get(), 2)
        assert event.type == EventType.HEARTBEAT
        assert event.data["spend_usd"] == pytest.approx(0.5)
        assert "agents" in event.data

    async def test_one_failed_beat_does_not_end_the_loop(self, session, monkeypatch):
        run_id = await _seed_run(session, queued=True, label="beat")
        orchestrator = RunOrchestrator(
            run_id, concurrency=1, skip_threshold=0.9, step_budget=5, limits=RunLimits(),
            use_browser=False,
        )
        monkeypatch.setattr(pool, "HEARTBEAT_SECONDS", 0.01)
        calls = 0

        async def flaky() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("the database blinked")

        monkeypatch.setattr(orchestrator, "_beat", flaky)
        task = asyncio.create_task(orchestrator._heartbeat_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        await task

        assert calls >= 3  # it kept going after the first one raised


class TestFailedRuns:
    async def test_a_crashed_run_keeps_its_spend_and_tells_the_monitor(self, session):
        run_id = await _running_run(session)
        queue = events.get_event_bus().subscribe(run_id)

        await service._mark_failed(run_id, "boom", RunLimits(spend_usd=2.25, tokens_in=7))

        session.expire_all()
        run = await session.get(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert float(run.spend_usd) == pytest.approx(2.25)
        assert run.tokens_in == 7
        event = await asyncio.wait_for(queue.get(), 2)
        assert event.type == EventType.RUN_FAILED
        assert event.data["error"] == "boom"

    async def test_an_unexpected_error_fails_the_school_instead_of_stranding_it(self, session):
        run_id = await _seed_run_with_sites(session, queued=True, label="err", domains=["a.edu"])
        site_run_id = await session.scalar(select(SiteRun.id).where(SiteRun.run_id == run_id))
        await session.execute(
            update(SiteRun).where(SiteRun.id == site_run_id).values(status=SiteRunStatus.RUNNING)
        )
        await session.commit()
        orchestrator = RunOrchestrator(
            run_id, concurrency=1, skip_threshold=0.9, step_budget=5, limits=RunLimits(),
            use_browser=False,
        )

        await orchestrator._mark_site_failed(site_run_id, ValueError("bad page"))

        session.expire_all()
        site_run = await session.get(SiteRun, site_run_id)
        run = await session.get(Run, run_id)
        assert site_run.status == SiteRunStatus.FAILED
        assert site_run.error_code == "worker_error"
        assert run.sites_failed == 1
