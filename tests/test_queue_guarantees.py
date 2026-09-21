"""The queue's guarantees, tried the ways they could be broken.

`test_run_queue` covers how the queue behaves when it is used correctly. These
cover what the client saw go wrong: several waiting runs starting together after
the first finished, a queue stuck behind a run whose process was gone, a launch
that failed after the run was claimed, and a retried run jumping the line.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agentscrape.config import settings
from agentscrape.db.enums import RunStatus, SiteRunStatus
from agentscrape.db.models import Run, Site, SiteRun
from agentscrape.domain.schemas import RunConfigIn, RunCreate
from agentscrape.orchestrator import scheduler, service
from agentscrape.orchestrator.pool import RunOrchestrator, active_run_ids, register, unregister

from .fixture_server import serve
from .test_run_queue import API, _FakeOrchestrator, _seed_run, _seed_run_with_sites

STALE = timedelta(seconds=scheduler.RUN_STALE_SECONDS + 30)


@pytest_asyncio.fixture
async def client(clean_tables, reset_global_engine):
    from agentscrape.api.main import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth(client):
    response = await client.post(f"{API}/auth/login", json={"password": "change-me"})
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def launched(monkeypatch):
    started: list[str] = []

    async def fake_launch(run_id: str, **kwargs):
        started.append(run_id)
        register(_FakeOrchestrator(run_id))
        return None

    monkeypatch.setattr("agentscrape.orchestrator.service.launch_run", fake_launch)
    yield started
    for run_id in started:
        unregister(run_id)


async def _set(session, run_id: str, **values) -> None:
    await session.execute(update(Run).where(Run.id == run_id).values(**values))
    await session.commit()


class TestTheDatabaseRefusesASecondRunningRun:
    async def test_two_running_queued_runs_cannot_exist(self, session):
        first = await _seed_run(session, queued=True, label="first")
        second = await _seed_run(session, queued=True, label="second")
        await _set(session, first, status=RunStatus.RUNNING)

        with pytest.raises(IntegrityError):
            await _set(session, second, status=RunStatus.RUNNING)
        await session.rollback()

    async def test_finished_and_waiting_runs_do_not_count(self, session):
        done = await _seed_run(session, queued=True, label="done")
        running = await _seed_run(session, queued=True, label="running")
        await _seed_run(session, queued=True, label="waiting")
        await _set(session, done, status=RunStatus.COMPLETED)
        await _set(session, running, status=RunStatus.RUNNING)

    async def test_unqueued_runs_are_not_limited(self, session):
        """Benchmarks and the CLI run several at once on purpose."""
        a = await _seed_run(session, queued=False, label="a")
        b = await _seed_run(session, queued=False, label="b")
        await _set(session, a, status=RunStatus.RUNNING)
        await _set(session, b, status=RunStatus.RUNNING)


class TestClaiming:
    async def test_a_run_owned_by_a_live_process_blocks_the_queue(self, session, launched):
        """Running in another process: not in this registry, but still beating."""
        running = await _seed_run(session, queued=True, label="elsewhere")
        await _seed_run(session, queued=True, label="waiting")
        await _set(
            session, running, status=RunStatus.RUNNING, heartbeat_at=datetime.now(UTC)
        )

        assert await scheduler.start_next_if_idle() is None
        assert launched == []

    async def test_an_abandoned_run_resumes_before_anything_newer_starts(
        self, session, launched
    ):
        abandoned = await _seed_run(session, queued=True, label="abandoned")
        newer = await _seed_run(session, queued=True, label="newer")
        await _set(
            session, abandoned, status=RunStatus.RUNNING, queue_rank=1,
            heartbeat_at=datetime.now(UTC) - STALE,
        )
        await _set(session, newer, queue_rank=2)

        assert await scheduler.start_next_if_idle() == abandoned
        assert launched == [abandoned]

    async def test_a_restart_does_not_strand_the_queue_behind_a_dead_run(
        self, session, launched
    ):
        """The supervisor is what notices, since nothing else calls the queue."""
        dead = await _seed_run(session, queued=True, label="dead")
        await _set(
            session, dead, status=RunStatus.RUNNING,
            heartbeat_at=datetime.now(UTC) - STALE,
        )

        task = asyncio.create_task(scheduler.supervise(interval=0.05))
        try:
            for _ in range(100):
                if launched:
                    break
                await asyncio.sleep(0.05)
        finally:
            task.cancel()

        assert launched == [dead]

    async def test_many_simultaneous_callers_start_exactly_one(self, session, monkeypatch):
        """Nothing registered, as if each caller were another process: the
        claim in the database is the only thing standing between them."""
        started: list[str] = []

        async def launch_without_registering(run_id: str, **kwargs):
            started.append(run_id)

        monkeypatch.setattr(
            "agentscrape.orchestrator.service.launch_run", launch_without_registering
        )
        for n in range(4):
            await _seed_run(session, queued=True, label=f"run {n}")

        results = await asyncio.gather(*(scheduler.start_next_if_idle() for _ in range(8)))

        assert len(started) == 1
        assert [r for r in results if r] == started

    async def test_a_launch_that_fails_returns_the_run_to_the_queue(
        self, session, monkeypatch
    ):
        run_id = await _seed_run(session, queued=True, label="unlucky")

        async def broken_launch(run_id: str, **kwargs):
            raise RuntimeError("could not start")

        monkeypatch.setattr("agentscrape.orchestrator.service.launch_run", broken_launch)
        assert await scheduler.start_next_if_idle() is None

        session.expire_all()
        run = await session.get(Run, run_id)
        assert run.status == RunStatus.PENDING  # not stuck "running"
        assert await scheduler.queue_position(session, run_id) == 1

        # ...and it starts normally once the fault is gone.
        started: list[str] = []

        async def fine_launch(run_id: str, **kwargs):
            started.append(run_id)
            register(_FakeOrchestrator(run_id))

        monkeypatch.setattr("agentscrape.orchestrator.service.launch_run", fine_launch)
        try:
            assert await scheduler.start_next_if_idle() == run_id
        finally:
            unregister(run_id)


class TestTheEndpoints:
    async def _create(self, client, auth, host: str):
        response = await client.post(
            f"{API}/runs", headers=auth, json={"sites": [host]}
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def test_three_runs_start_one_and_queue_two(self, client, auth, launched):
        first = await self._create(client, auth, "a.example.edu")
        second = await self._create(client, auth, "b.example.edu")
        third = await self._create(client, auth, "c.example.edu")

        assert launched == [first["id"]]
        assert first["queue_position"] is None
        assert (second["queue_position"], third["queue_position"]) == (1, 2)

    async def test_a_crawl_is_named_after_its_school_and_the_day(
        self, client, auth, launched
    ):
        run = await self._create(client, auth, "med.example.edu")
        assert run["label"].startswith("med.example.edu")
        assert f"{datetime.now(UTC):%Y}" in run["label"]

    async def test_a_name_the_client_gave_is_kept(self, client, auth, launched):
        response = await client.post(
            f"{API}/runs", headers=auth,
            json={"sites": ["med.example.edu"], "config": {"label": "Spring sweep"}},
        )
        assert response.json()["label"] == "Spring sweep"

    async def test_the_list_carries_each_crawls_school(self, client, auth, launched):
        await self._create(client, auth, "med.example.edu")
        body = (await client.get(f"{API}/runs", headers=auth)).json()
        assert body["items"][0]["school_name"] == "med.example.edu"

    async def test_a_waiting_run_can_be_moved_and_removed(self, client, auth, launched):
        running = await self._create(client, auth, "a.example.edu")
        b = await self._create(client, auth, "b.example.edu")
        c = await self._create(client, auth, "c.example.edu")
        d = await self._create(client, auth, "d.example.edu")

        def order(view):
            return [e["run_id"] for e in view["waiting"]]

        moved = await client.post(f"{API}/runs/{d['id']}/move", headers=auth, json={"direction": "top"})
        assert order(moved.json()) == [d["id"], b["id"], c["id"]]

        moved = await client.post(f"{API}/runs/{d['id']}/move", headers=auth, json={"direction": "down"})
        assert order(moved.json()) == [b["id"], d["id"], c["id"]]

        moved = await client.post(f"{API}/runs/{c['id']}/move", headers=auth, json={"direction": "up"})
        assert order(moved.json()) == [b["id"], c["id"], d["id"]]
        assert [e["position"] for e in moved.json()["waiting"]] == [1, 2, 3]

        removed = await client.post(f"{API}/runs/{c['id']}/cancel", headers=auth)
        assert removed.json()["status"] == RunStatus.CANCELLED
        view = (await client.get(f"{API}/runs/queue", headers=auth)).json()
        assert order(view) == [b["id"], d["id"]]
        # Removing a waiting run never starts anything: the first is still going.
        assert launched == [running["id"]]

    async def test_the_run_in_progress_cannot_be_moved(self, client, auth, launched):
        running = await self._create(client, auth, "a.example.edu")
        await self._create(client, auth, "b.example.edu")
        # It is running for the database as well as the registry.
        response = await client.post(
            f"{API}/runs/{running['id']}/move", headers=auth, json={"direction": "down"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RUN_NOT_WAITING"

    async def test_a_retried_run_goes_to_the_back(self, client, auth, launched, session):
        """It used to keep its old creation time and jump ahead of newer runs."""
        finished = await self._create(client, auth, "a.example.edu")
        unregister(finished["id"])
        waiting = await self._create(client, auth, "b.example.edu")
        site_id = (
            await session.execute(select(SiteRun.site_id).where(SiteRun.run_id == finished["id"]))
        ).scalar_one()
        await _set(session, finished["id"], status=RunStatus.COMPLETED)
        await session.execute(
            update(SiteRun).where(SiteRun.run_id == finished["id"]).values(status=SiteRunStatus.FAILED)
        )
        await session.commit()
        # Something else holds the model budget, so the retry has to wait.
        register(_FakeOrchestrator("holding-the-budget"))
        try:
            response = await client.post(
                f"{API}/runs/{finished['id']}/sites/{site_id}/retry", headers=auth
            )
            assert response.status_code == 200, response.text
            view = (await client.get(f"{API}/runs/queue", headers=auth)).json()
        finally:
            unregister("holding-the-budget")

        assert [e["run_id"] for e in view["waiting"]] == [waiting["id"], finished["id"]]


class TestLaunching:
    async def test_a_queued_run_crawls_one_school_at_a_time(self, session, monkeypatch):
        """Several schools in one run used to be crawled four at once."""
        run_id = await _seed_run_with_sites(
            session, queued=True, label="many", domains=["a.edu", "b.edu", "c.edu"]
        )
        await _set(session, run_id, config={"concurrency": 4, "queued": True})
        seen: dict = {}

        class Recorder:
            def __init__(self, run_id, **kwargs):
                seen.update(kwargs)
                self.run_id = run_id

            async def start(self):
                return None

        monkeypatch.setattr(service, "RunOrchestrator", Recorder)
        await service.launch_run(run_id)
        await asyncio.sleep(0.1)
        unregister(run_id)

        assert seen["concurrency"] == 1

    async def test_a_resumed_run_keeps_what_it_had_already_spent(self, session, monkeypatch):
        run_id = await _seed_run(session, queued=True, label="resumed")
        await _set(session, run_id, spend_usd=0.75, tokens_in=1200, tokens_out=300)
        seen: dict = {}

        class Recorder:
            def __init__(self, run_id, **kwargs):
                seen.update(kwargs)
                self.run_id = run_id

            async def start(self):
                return None

        monkeypatch.setattr(service, "RunOrchestrator", Recorder)
        await service.launch_run(run_id)
        await asyncio.sleep(0.1)
        unregister(run_id)

        limits = seen["limits"]
        assert (limits.spend_usd, limits.tokens_in, limits.tokens_out) == (0.75, 1200, 300)


class TestCounters:
    async def test_spend_and_people_are_saved_while_the_crawl_is_going(self, session):
        run_id = await _seed_run_with_sites(session, queued=True, label="live", domains=["a.edu"])
        from agentscrape.orchestrator.limits import RunLimits, SiteCounts

        limits = RunLimits(spend_usd=0.5, tokens_in=10, tokens_out=5)
        orchestrator = RunOrchestrator(
            run_id, concurrency=1, skip_threshold=0.9, step_budget=5, limits=limits,
            use_browser=False,
        )
        site_run_id = (await session.execute(select(SiteRun.id))).scalar_one()
        await orchestrator._counts_hook(site_run_id)(SiteCounts(people=7, trainees=3, emails=1))
        await limits.add_usage(100, 50, 0.25)

        await orchestrator._persist_totals(beat=True)

        session.expire_all()
        run = await session.get(Run, run_id)
        assert float(run.spend_usd) == pytest.approx(0.75)
        assert (run.tokens_in, run.tokens_out) == (110, 55)
        assert run.records_found == 7  # the school has not finished
        assert run.heartbeat_at is not None

    async def test_counting_a_school_twice_does_not_double_the_totals(self, session):
        """Retrying a school, or resuming a run, re-runs it through the same path."""
        run_id = await _seed_run_with_sites(
            session, queued=True, label="retry", domains=["a.edu", "b.edu"]
        )
        from agentscrape.orchestrator.limits import RunLimits

        await session.execute(
            update(SiteRun).where(SiteRun.run_id == run_id).values(
                status=SiteRunStatus.COMPLETED, records_found=10, records_new=4
            )
        )
        await session.commit()
        orchestrator = RunOrchestrator(
            run_id, concurrency=1, skip_threshold=0.9, step_budget=5, limits=RunLimits(),
            use_browser=False,
        )

        for _ in range(3):
            await orchestrator._persist_totals()

        session.expire_all()
        run = await session.get(Run, run_id)
        assert (run.sites_completed, run.records_found, run.records_new) == (2, 20, 8)


class TestThreeRunsEndToEnd:
    """The client's complaint, reproduced against real crawls of local fixtures."""

    async def test_queued_crawls_run_one_after_another_in_order(
        self, session, monkeypatch
    ):
        monkeypatch.setattr(settings, "enable_crt_sh", False)
        monkeypatch.setattr(settings, "requests_per_second_per_domain", 50.0)
        monkeypatch.setattr(settings, "discovery_timeout_seconds", 30)
        monkeypatch.setattr(settings, "discovery_source_timeout_seconds", 10)
        from agentscrape.browser import ratelimit

        monkeypatch.setattr(ratelimit, "_limiter", None)

        real = service.RunOrchestrator

        def without_browser(run_id, **kwargs):
            return real(run_id, **{**kwargs, "use_browser": False})

        monkeypatch.setattr(service, "RunOrchestrator", without_browser)

        with serve("first.localhost") as one, serve("second.localhost") as two, serve(
            "third.localhost"
        ) as three:
            ids = []
            for site in (one, two, three):
                run = await service.create_run(
                    session,
                    RunCreate(
                        sites=[site.base],
                        config=RunConfigIn(step_budget=20, modes=["crawl"]),
                    ),
                )
                ids.append(run.id)

            peak_running = 0
            peak_registered = 0
            stop = asyncio.Event()

            async def watch() -> None:
                nonlocal peak_running, peak_registered
                from agentscrape.db.session import get_sessionmaker

                while not stop.is_set():
                    async with get_sessionmaker()() as own:
                        running = await own.scalar(
                            select(func.count(Run.id)).where(Run.status == RunStatus.RUNNING)
                        )
                    peak_running = max(peak_running, int(running or 0))
                    peak_registered = max(peak_registered, len(active_run_ids()))
                    await asyncio.sleep(0.01)

            watcher = asyncio.create_task(watch())
            try:
                assert await scheduler.start_next_if_idle() == ids[0]
                for _ in range(600):
                    session.expire_all()
                    runs = [await session.get(Run, i) for i in ids]
                    if all(r.status == RunStatus.COMPLETED for r in runs):
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(f"runs did not finish: {[r.status for r in runs]}")
            finally:
                stop.set()
                await watcher

        assert peak_running == 1
        assert peak_registered == 1
        session.expire_all()
        runs = [await session.get(Run, i) for i in ids]
        starts = [r.started_at for r in runs]
        finishes = [r.finished_at for r in runs]
        assert starts == sorted(starts)
        # Each began only after the one before it had ended.
        assert finishes[0] <= starts[1] and finishes[1] <= starts[2]
        assert all(r.records_found >= 3 for r in runs)
