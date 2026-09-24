"""Verification waits its turn in the run queue, and says why when it can't read a page.

A verification pass reads every source page again and calls the model for
each, the same budget a crawl spends, so it is a queued run of kind `verify`
rather than a task started beside whatever crawl is going. And a page plain
HTTP is refused (as many hospital sites do from a cloud server's address) is
opened in a browser before the record is given up on - never reported as a
pass that checked nothing and found nothing to fix.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from agentscrape.db.enums import (
    RUN_KIND_VERIFY,
    ExtractionMethod,
    FetchMode,
    PersonCategory,
    RecordStatus,
    RunStatus,
    VerificationStatus,
)
from agentscrape.db.models import Record, RecordVersion, Run, Site, VerificationJob
from agentscrape.orchestrator import scheduler
from agentscrape.orchestrator.pool import active_run_ids, register, unregister
from agentscrape.verification import service as verification

API = "/api/v1"


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
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id


async def _seed_people(session, *, fetch_mode=FetchMode.HTML) -> list[str]:
    site = Site(root_domain="med.example.edu", canonical_url="https://med.example.edu/", name="Example Medical Center")
    session.add(site)
    await session.flush()
    now = datetime.now(UTC)
    ids = []
    for name in ("Naomi Goldrich", "Chad Caraway"):
        record = Record(
            site_id=site.id, identity_key=f"name:{name}", identity_kind="name",
            full_name=name, category=PersonCategory.RESIDENT, position="Resident",
            status=RecordStatus.ACTIVE, confidence=0.9, version_count=1, last_changed_at=now,
        )
        session.add(record)
        await session.flush()
        version = RecordVersion(
            record_id=record.id, version_no=1, fields={"full_name": name}, changed_fields={},
            source_url="https://med.example.edu/residents", page_title="Residents",
            captured_at=now, extraction_method=ExtractionMethod.DISCOVERY,
            fetch_mode=fetch_mode, confidence=0.9,
        )
        session.add(version)
        await session.flush()
        record.current_version_id = version.id
        ids.append(record.id)
    await session.commit()
    return ids


async def _job(session, job_id: str) -> VerificationJob:
    session.expire_all()
    return await session.get(VerificationJob, job_id)


async def _wait_idle(timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if not active_run_ids():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"still running: {active_run_ids()}")


class TestQueued:
    async def test_verify_waits_behind_a_running_crawl(self, client, auth, session):
        ids = await _seed_people(session)
        crawl = Run(status=RunStatus.RUNNING, queued=True, config={}, sites_total=1,
                    heartbeat_at=datetime.now(UTC))
        session.add(crawl)
        await session.commit()
        register(_FakeOrchestrator(crawl.id))
        try:
            response = await client.post(f"{API}/people/verify", json={"record_ids": ids}, headers=auth)
            assert response.status_code == 202, response.text
            body = response.json()
            assert body["status"] == "pending"
            assert body["queue_position"] == 1
            assert body["run_id"]

            queue = (await client.get(f"{API}/runs/queue", headers=auth)).json()
            assert [e["kind"] for e in queue["waiting"]] == [RUN_KIND_VERIFY]
            assert queue["waiting"][0]["label"] == "Verify 2 rows — Example Medical Center"
            assert queue["waiting"][0]["records_total"] == 0

            # Verification runs are not past crawls.
            history = (await client.get(f"{API}/runs", headers=auth)).json()
            assert [r["id"] for r in history["items"]] == [crawl.id]
        finally:
            unregister(crawl.id)

    async def test_it_runs_when_its_turn_comes_and_hands_the_queue_on(
        self, session, monkeypatch
    ):
        ids = await _seed_people(session)

        async def fake_run(job_id, **kwargs):
            async with verification.session_scope() as s:
                job = await s.get(VerificationJob, job_id)
                job.status = VerificationStatus.COMPLETED
                job.records_total = job.records_checked = len(ids)

        monkeypatch.setattr(verification, "run_verification", fake_run)
        from agentscrape.domain.schemas import VerificationCreate

        job = await verification.create_verification_job(session, VerificationCreate(record_ids=ids))
        after = Run(status=RunStatus.PENDING, queued=True, config={}, sites_total=1,
                    queue_rank=await scheduler.next_rank(session))
        session.add(after)
        await session.commit()
        after_id = after.id

        launched_next: list[str] = []

        async def fake_launch_crawl(run_id, **kwargs):
            async with verification.session_scope() as s:
                kind = (await s.get(Run, run_id)).kind
            if kind == RUN_KIND_VERIFY:
                await verification.launch_verification_run(run_id)
            else:
                launched_next.append(run_id)

        monkeypatch.setattr("agentscrape.orchestrator.service.launch_run", fake_launch_crawl)
        assert await scheduler.start_next_if_idle() == job.run_id
        for _ in range(250):
            if launched_next:
                break
            await asyncio.sleep(0.02)

        assert launched_next == [after_id]
        run_id = job.run_id
        session.expire_all()
        run = await session.get(Run, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.finished_at is not None

    async def test_a_whole_site_pass_is_not_queued_twice(self, session):
        await _seed_people(session)
        site_id = (await session.execute(Site.__table__.select())).first().id
        from agentscrape.domain.schemas import VerificationCreate

        first = await verification.create_verification_job(session, VerificationCreate(site_id=site_id))
        second = await verification.create_verification_job(session, VerificationCreate(site_id=site_id))
        assert first.id == second.id

    async def test_removing_a_waiting_pass_ends_its_job(self, client, auth, session):
        ids = await _seed_people(session)
        blocker = Run(status=RunStatus.RUNNING, queued=True, config={}, sites_total=1,
                      heartbeat_at=datetime.now(UTC))
        session.add(blocker)
        await session.commit()
        register(_FakeOrchestrator(blocker.id))
        try:
            job = (await client.post(f"{API}/people/verify", json={"record_ids": ids}, headers=auth)).json()
            response = await client.post(f"{API}/runs/{job['run_id']}/cancel", headers=auth)
            assert response.status_code == 200
            after = (await client.get(f"{API}/people/verify/{job['id']}", headers=auth)).json()
            assert after["status"] == "failed"
            assert "removed from the queue" in after["error"]
        finally:
            unregister(blocker.id)

    async def test_stopping_a_running_pass_ends_job_and_run(self, session, monkeypatch):
        ids = await _seed_people(session)
        started = asyncio.Event()

        async def slow_run(job_id, **kwargs):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(verification, "run_verification", slow_run)
        from agentscrape.domain.schemas import VerificationCreate
        from agentscrape.orchestrator import service

        job = await verification.create_verification_job(session, VerificationCreate(record_ids=ids))
        assert await scheduler.start_next_if_idle() == job.run_id
        await asyncio.wait_for(started.wait(), 5)

        await service.cancel_run(session, job.run_id)
        await _wait_idle()
        assert (await _job(session, job.id)).status == VerificationStatus.FAILED
        run = await session.get(Run, job.run_id)
        await session.refresh(run)
        assert run.status == RunStatus.CANCELLED


class TestReadingPages:
    @pytest.fixture
    def model_confirms(self, monkeypatch):
        """The model grounds everyone it was asked about as a fellow."""
        seen: list[str] = []

        async def fake_verify(*, url, title, text, people, meter=None, provider=None):
            seen.append(text)
            return {p.record_id: "fellow" for p in people if p.full_name in text}

        monkeypatch.setattr(verification, "verify_page_roles", fake_verify)
        return seen

    @pytest.fixture
    def plain_http_refused(self, monkeypatch):
        async def refused(fetcher, url):
            return None, "HTTP 403"

        monkeypatch.setattr(verification, "_read_plain", refused)

    async def _run(self, session, ids) -> VerificationJob:
        job = VerificationJob(record_ids=ids)
        session.add(job)
        await session.commit()
        await verification.run_verification(job.id)
        return await _job(session, job.id)

    async def test_a_refused_page_is_read_in_a_browser(
        self, session, monkeypatch, model_confirms, plain_http_refused
    ):
        ids = await _seed_people(session)

        async def rendered(self, url):
            return "Naomi Goldrich, Chad Caraway - fellows", "Residents", None

        monkeypatch.setattr(verification._Browser, "read", rendered)
        job = await self._run(session, ids)
        assert job.status == VerificationStatus.COMPLETED
        assert (job.records_checked, job.records_corrected) == (2, 2)
        for record_id in ids:
            record = await session.get(Record, record_id)
            await session.refresh(record)
            assert record.category == "fellow"
            assert record.roles == ["fellow"]
            assert record.version_count == 2
            version = await session.get(RecordVersion, record.current_version_id)
            assert version.fields["category"] == "fellow"
            assert version.changed_fields["category"] == {"from": "resident", "to": "fellow"}
            assert version.extraction_method == "verify"
        # Re-verifying an unchanged label must not generate another version.
        await self._run(session, ids)
        for record_id in ids:
            record = await session.get(Record, record_id)
            await session.refresh(record)
            assert record.version_count == 2

    async def test_a_page_nothing_can_read_fails_with_the_reason(
        self, session, monkeypatch, model_confirms, plain_http_refused
    ):
        ids = await _seed_people(session)

        async def no_browser(self, url):
            return None, "", "browser: HTTP 403"

        monkeypatch.setattr(verification._Browser, "read", no_browser)
        job = await self._run(session, ids)
        assert job.status == VerificationStatus.FAILED
        assert job.records_checked == 0
        assert "HTTP 403" in job.error
        assert "checked 0 of 2" in job.error

    async def test_a_page_the_crawl_rendered_goes_straight_to_the_browser(
        self, session, monkeypatch, model_confirms
    ):
        ids = await _seed_people(session, fetch_mode=FetchMode.RENDER)

        async def must_not_fetch(fetcher, url):
            raise AssertionError("plain HTTP was tried for a page the crawl had to render")

        async def rendered(self, url):
            return "Naomi Goldrich and Chad Caraway", "Residents", None

        monkeypatch.setattr(verification, "_read_plain", must_not_fetch)
        monkeypatch.setattr(verification._Browser, "read", rendered)
        job = await self._run(session, ids)
        assert job.status == VerificationStatus.COMPLETED
        assert job.records_checked == 2

    async def test_a_model_that_never_answers_is_a_failure_not_a_clean_pass(
        self, session, monkeypatch
    ):
        ids = await _seed_people(session)

        async def page(fetcher, url):
            return "Naomi Goldrich, Chad Caraway", None

        monkeypatch.setattr(verification, "_read_plain", page)
        # The conftest's offline provider fails every model call.
        job = await self._run(session, ids)
        assert job.status == VerificationStatus.FAILED
        assert "no usable answer from the model" in job.error
        assert "offline" in job.error
