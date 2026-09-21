"""Queued runs start one at a time.

`llm_concurrency` is a single process-wide gate, so runs started side by side
split the model throughput between them while each still pays its own
discovery and link-ranking startup. These cover the property that buys back:
a queued run waits for the process to go idle, and exactly one starts when it
does — including after a restart, which must not release the whole queue.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentscrape.db.enums import RunStatus
from agentscrape.db.models import Run
from agentscrape.orchestrator import scheduler
from agentscrape.orchestrator.pool import register, unregister

API = "/api/v1"


async def _seed_run(session, *, queued: bool, label: str) -> str:
    run = Run(
        status=RunStatus.PENDING,
        label=label,
        config={"concurrency": 1, "queued": queued},
        sites_total=1,
    )
    session.add(run)
    await session.commit()
    return run.id


@pytest_asyncio.fixture
async def client(clean_tables, reset_global_engine):
    from agentscrape.api.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth(client):
    response = await client.post(f"{API}/auth/login", json={"password": "change-me"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


class _FakeOrchestrator:
    """Stands in for a run in flight: the scheduler only reads the registry."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


@pytest.fixture
def launched(monkeypatch):
    """Record what the scheduler launches instead of starting real crawls."""
    started: list[str] = []

    async def fake_launch(run_id: str, **kwargs):
        started.append(run_id)
        register(_FakeOrchestrator(run_id))  # as a real launch would
        return None

    monkeypatch.setattr("agentscrape.orchestrator.service.launch_run", fake_launch)
    yield started
    for run_id in started:
        unregister(run_id)


class TestQueuePosition:
    async def test_position_is_fifo_by_creation(self, session):
        first = await _seed_run(session, queued=True, label="first")
        second = await _seed_run(session, queued=True, label="second")

        assert await scheduler.queue_position(session, first) == 1
        assert await scheduler.queue_position(session, second) == 2

    async def test_an_unqueued_run_has_no_position(self, session):
        run_id = await _seed_run(session, queued=False, label="immediate")
        assert await scheduler.queue_position(session, run_id) is None

    async def test_a_finished_run_leaves_the_queue(self, session):
        first = await _seed_run(session, queued=True, label="first")
        second = await _seed_run(session, queued=True, label="second")

        run = await session.get(Run, first)
        run.status = RunStatus.COMPLETED
        await session.commit()

        assert await scheduler.queue_position(session, first) is None
        assert await scheduler.queue_position(session, second) == 1


class TestStarting:
    async def test_the_oldest_queued_run_starts_when_idle(self, session, launched):
        first = await _seed_run(session, queued=True, label="first")
        await _seed_run(session, queued=True, label="second")

        assert await scheduler.start_next_if_idle() == first
        assert launched == [first]

    async def test_nothing_starts_while_a_run_is_active(self, session, launched):
        await _seed_run(session, queued=True, label="waiting")
        register(_FakeOrchestrator("some-other-run"))
        try:
            assert await scheduler.start_next_if_idle() is None
            assert launched == []
        finally:
            unregister("some-other-run")

    async def test_only_one_starts_per_idle_moment(self, session, launched):
        """The second call sees the first run registered and holds off."""
        first = await _seed_run(session, queued=True, label="first")
        await _seed_run(session, queued=True, label="second")

        assert await scheduler.start_next_if_idle() == first
        assert await scheduler.start_next_if_idle() is None
        assert launched == [first]

    async def test_the_next_run_starts_when_the_previous_finishes(
        self, session, launched
    ):
        first = await _seed_run(session, queued=True, label="first")
        second = await _seed_run(session, queued=True, label="second")

        assert await scheduler.start_next_if_idle() == first

        # The finished run leaves the registry and the live statuses, exactly
        # as `service.launch_run` does in its `finally`.
        unregister(first)
        run = await session.get(Run, first)
        run.status = RunStatus.COMPLETED
        await session.commit()

        await scheduler.on_run_finished(first)
        assert launched == [first, second]

    async def test_an_empty_queue_is_a_no_op(self, session, launched):
        await _seed_run(session, queued=False, label="not queued")
        assert await scheduler.start_next_if_idle() is None
        assert launched == []

    async def test_a_crashed_queued_run_is_picked_up_again(self, session, launched):
        """Left RUNNING by a killed process, absent from the registry."""
        run_id = await _seed_run(session, queued=True, label="crashed")
        run = await session.get(Run, run_id)
        run.status = RunStatus.RUNNING
        await session.commit()

        assert await scheduler.start_next_if_idle() == run_id


class TestResume:
    async def test_a_restart_does_not_release_the_whole_queue(self, session, launched):
        """The point of queueing is lost if a restart starts every run."""
        from agentscrape.orchestrator import service

        first = await _seed_run(session, queued=True, label="first")
        await _seed_run(session, queued=True, label="second")
        await _seed_run(session, queued=True, label="third")

        resumed = await service.resume_interrupted_runs()

        assert resumed == [first]
        assert launched == [first]

    async def test_unqueued_runs_still_resume_together(self, session, launched):
        from agentscrape.orchestrator import service

        first = await _seed_run(session, queued=False, label="first")
        second = await _seed_run(session, queued=False, label="second")

        resumed = await service.resume_interrupted_runs()

        assert set(resumed) == {first, second}


class TestCreateEndpoint:
    """The queue as a client sees it: create, wait, report a position."""

    async def test_a_queued_run_waits_while_another_is_active(
        self, client, auth, launched, session
    ):
        register(_FakeOrchestrator("already-running"))
        try:
            response = await client.post(
                f"{API}/runs",
                headers=auth,
                json={"sites": ["med.example.edu"], "config": {"queued": True}},
            )
        finally:
            unregister("already-running")

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == RunStatus.PENDING
        assert body["queued"] is True
        assert body["queue_position"] == 1
        assert launched == []  # it did not start beside the active run

    async def test_a_queued_run_starts_at_once_when_idle(
        self, client, auth, launched
    ):
        response = await client.post(
            f"{API}/runs",
            headers=auth,
            json={"sites": ["med.example.edu"], "config": {"queued": True}},
        )
        assert response.status_code == 201
        assert launched == [response.json()["id"]]

    async def test_an_unqueued_run_starts_regardless(self, client, auth, launched):
        register(_FakeOrchestrator("already-running"))
        try:
            response = await client.post(
                f"{API}/runs",
                headers=auth,
                json={"sites": ["med.example.edu"], "config": {"queued": False}},
            )
        finally:
            unregister("already-running")

        assert response.status_code == 201
        assert launched == [response.json()["id"]]
