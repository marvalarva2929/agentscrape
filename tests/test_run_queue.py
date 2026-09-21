"""Queued runs start one at a time.

`llm_concurrency` is a single process-wide gate, so runs started side by side
split the model throughput between them while each still pays its own
discovery and link-ranking startup. These cover the property that buys back:
a queued run waits for the process to go idle, and exactly one starts when it
does — including after a restart, which must not release the whole queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentscrape.db.enums import RunStatus, SiteRunStatus
from agentscrape.db.models import Run, Site, SiteRun
from agentscrape.orchestrator import scheduler
from agentscrape.orchestrator.pool import register, unregister
from agentscrape.orchestrator.queue import STALE_CLAIM_MINUTES

API = "/api/v1"


async def _seed_run(session, *, queued: bool, label: str) -> str:
    run = Run(
        status=RunStatus.PENDING,
        label=label,
        config={"concurrency": 1, "queued": queued},
        queued=queued,
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

    async def test_runs_from_before_queueing_join_the_queue_one_at_a_time(
        self, session, launched
    ):
        """An interrupted run that was never queued used to relaunch beside the
        others on every restart. Now it takes its place in line."""
        from agentscrape.orchestrator import service

        first = await _seed_run(session, queued=False, label="first")
        second = await _seed_run(session, queued=False, label="second")

        resumed = await service.resume_interrupted_runs()

        assert resumed == [first]
        assert launched == [first]
        session.expire_all()
        assert (await session.get(Run, second)).queued is True
        assert await scheduler.queue_position(session, second) == 1


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

    async def test_asking_not_to_queue_does_not_skip_the_line(
        self, client, auth, launched
    ):
        """A client (or an old cached page) that sends queued=false used to start
        a second crawl beside the running one. The server queues it anyway."""
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
        assert response.json()["queued"] is True
        assert response.json()["queue_position"] == 1
        assert launched == []


async def _seed_run_with_sites(session, *, queued: bool, label: str, domains: list[str]):
    """A run plus one SiteRun per domain, as `create_run` would build it."""
    run = Run(
        status=RunStatus.PENDING,
        label=label,
        config={"concurrency": 1, "queued": queued},
        queued=queued,
        sites_total=len(domains),
    )
    session.add(run)
    await session.flush()
    for position, domain in enumerate(domains):
        site = Site(root_domain=domain, canonical_url=f"https://{domain}/")
        session.add(site)
        await session.flush()
        session.add(
            SiteRun(
                run_id=run.id,
                site_id=site.id,
                status=SiteRunStatus.PENDING,
                step_budget=5000,
                position=position,
            )
        )
    await session.commit()
    return run.id


class TestQueueView:
    async def test_waiting_runs_are_numbered_in_order(self, session):
        first = await _seed_run_with_sites(
            session, queued=True, label="first", domains=["a.edu"]
        )
        second = await _seed_run_with_sites(
            session, queued=True, label="second", domains=["b.edu"]
        )

        view = await scheduler.queue_view(session)

        assert [e.run_id for e in view.waiting] == [first, second]
        assert [e.position for e in view.waiting] == [1, 2]
        assert view.running == []

    async def test_a_running_run_is_separated_and_unnumbered(self, session):
        running = await _seed_run_with_sites(
            session, queued=True, label="running", domains=["a.edu"]
        )
        waiting = await _seed_run_with_sites(
            session, queued=True, label="waiting", domains=["b.edu"]
        )
        register(_FakeOrchestrator(running))
        try:
            view = await scheduler.queue_view(session)
        finally:
            unregister(running)

        assert [e.run_id for e in view.running] == [running]
        assert view.running[0].position is None
        assert view.running[0].running is True
        # The waiting run is next in line, not second behind the running one.
        assert [(e.run_id, e.position) for e in view.waiting] == [(waiting, 1)]

    async def test_each_school_is_listed_with_its_progress(self, session):
        await _seed_run_with_sites(
            session, queued=True, label="two schools",
            domains=["gme.uchicago.edu", "med.virginia.edu"],
        )

        view = await scheduler.queue_view(session)

        entry = view.waiting[0]
        assert {s.domain for s in entry.sites} == {
            "gme.uchicago.edu", "med.virginia.edu",
        }
        assert entry.sites_total == 2
        assert entry.sites_pending == 2
        assert all(s.status == SiteRunStatus.PENDING for s in entry.sites)
        assert all(s.step_budget == 5000 for s in entry.sites)

    async def test_the_school_order_is_stable_between_calls(self, session):
        """Schools created with their run share one timestamp, so the view
        needs a tiebreaker or it would reshuffle on every poll."""
        await _seed_run_with_sites(
            session, queued=True, label="many",
            domains=[f"school{i}.edu" for i in range(8)],
        )

        first = await scheduler.queue_view(session)
        second = await scheduler.queue_view(session)

        assert [s.domain for s in first.waiting[0].sites] == [
            s.domain for s in second.waiting[0].sites
        ]

    async def test_a_run_left_behind_by_a_dead_process_is_reported(self, session):
        """Neither running nor queued: visible, so a stuck queue is explicable."""
        run_id = await _seed_run_with_sites(
            session, queued=False, label="stalled", domains=["a.edu"]
        )
        run = await session.get(Run, run_id)
        run.status = RunStatus.RUNNING
        await session.commit()

        view = await scheduler.queue_view(session)

        assert [e.run_id for e in view.stalled] == [run_id]
        assert view.running == [] and view.waiting == []

    async def test_finished_runs_are_absent(self, session):
        run_id = await _seed_run_with_sites(
            session, queued=True, label="done", domains=["a.edu"]
        )
        run = await session.get(Run, run_id)
        run.status = RunStatus.COMPLETED
        await session.commit()

        view = await scheduler.queue_view(session)
        assert view.running == [] and view.waiting == [] and view.stalled == []


class TestQueueEndpoint:
    async def test_the_queue_is_served_in_order(self, client, auth, session):
        first = await _seed_run_with_sites(
            session, queued=True, label="first", domains=["gme.uchicago.edu"]
        )
        await _seed_run_with_sites(
            session, queued=True, label="second", domains=["med.virginia.edu"]
        )

        response = await client.get(f"{API}/runs/queue", headers=auth)

        assert response.status_code == 200
        body = response.json()
        assert [e["position"] for e in body["waiting"]] == [1, 2]
        assert body["waiting"][0]["run_id"] == first
        assert body["waiting"][0]["label"] == "first"
        assert body["waiting"][0]["sites"][0]["domain"] == "gme.uchicago.edu"

    async def test_queue_is_not_read_as_a_run_id(self, client, auth):
        """The literal path must win over /runs/{run_id}."""
        response = await client.get(f"{API}/runs/queue", headers=auth)
        assert response.status_code == 200
        assert "waiting" in response.json()

    async def test_the_queue_needs_authentication(self, client):
        assert (await client.get(f"{API}/runs/queue")).status_code == 401


class TestQueueViewAcrossProcesses:
    """The CLI reads the queue from a different process than the API, where
    the in-process registry is always empty. Classification must not depend
    on it, or every running run would be reported as merely waiting."""

    async def _beat(self, session, run_id: str, when) -> None:
        from sqlalchemy import update

        await session.execute(
            update(SiteRun).where(SiteRun.run_id == run_id).values(
                status=SiteRunStatus.RUNNING, heartbeat_at=when
            )
        )
        await session.commit()

    async def test_a_recent_heartbeat_reads_as_running(self, session):
        run_id = await _seed_run_with_sites(
            session, queued=True, label="live", domains=["a.edu"]
        )
        run = await session.get(Run, run_id)
        run.status = RunStatus.RUNNING
        await session.commit()
        await self._beat(session, run_id, datetime.now(UTC))

        view = await scheduler.queue_view(session)  # registry empty, as in the CLI

        assert [e.run_id for e in view.running] == [run_id]
        assert view.running[0].running is True
        assert view.stalled == []

    async def test_a_stale_heartbeat_reads_as_stalled(self, session):
        run_id = await _seed_run_with_sites(
            session, queued=True, label="dead", domains=["a.edu"]
        )
        run = await session.get(Run, run_id)
        run.status = RunStatus.RUNNING
        await session.commit()
        await self._beat(
            session, run_id,
            datetime.now(UTC) - timedelta(minutes=STALE_CLAIM_MINUTES + 5),
        )

        view = await scheduler.queue_view(session)

        assert [e.run_id for e in view.stalled] == [run_id]
        assert view.running == []

    async def test_waiting_runs_are_numbered_behind_a_running_one(self, session):
        running = await _seed_run_with_sites(
            session, queued=True, label="running", domains=["a.edu"]
        )
        waiting = await _seed_run_with_sites(
            session, queued=True, label="waiting", domains=["b.edu"]
        )
        run = await session.get(Run, running)
        run.status = RunStatus.RUNNING
        await session.commit()
        await self._beat(session, running, datetime.now(UTC))

        view = await scheduler.queue_view(session)

        assert [(e.run_id, e.position) for e in view.waiting] == [(waiting, 1)]
