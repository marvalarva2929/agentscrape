"""Queue claiming, resume, cancellation, hard stops and the memory ceiling.

Section 9 calls concurrency the hardest part of the system, so these target the
properties that are easy to get subtly wrong: two workers must never claim the
same site, a resumed run must not reprocess completed work, and a run that hits
a limit must stop cleanly with its partial results intact.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentscrape.db.enums import RunStatus, SiteRunStatus, StopReason
from agentscrape.db.models import Run, Site, SiteRun
from agentscrape.orchestrator.limits import (
    MemoryCeilingExceeded,
    RunLimits,
    check_memory_ceiling,
)
from agentscrape.orchestrator.queue import (
    cancel_pending,
    claim_next_site,
    pending_count,
    reset_running_for_resume,
)


async def _seed_run(session, site_count: int = 6) -> str:
    run = Run(status=RunStatus.PENDING, sites_total=site_count, config={"concurrency": 3})
    session.add(run)
    await session.flush()
    for i in range(site_count):
        site = Site(
            root_domain=f"hospital{i}.example.edu",
            canonical_url=f"https://hospital{i}.example.edu/",
        )
        session.add(site)
        await session.flush()
        session.add(
            SiteRun(run_id=run.id, site_id=site.id, status=SiteRunStatus.PENDING)
        )
    await session.commit()
    return run.id


class TestQueue:
    async def test_claiming_marks_the_site_running(self, session):
        run_id = await _seed_run(session, 2)
        claim = await claim_next_site(session, run_id, "agent-0")
        assert claim is not None
        site_run_id, _, url, _, _ = claim
        assert url.startswith("https://hospital")

        site_run = await session.get(SiteRun, site_run_id)
        await session.refresh(site_run)
        assert site_run.status == SiteRunStatus.RUNNING
        assert site_run.agent_id == "agent-0"

    async def test_empty_queue_returns_none(self, session):
        run_id = await _seed_run(session, 1)
        assert await claim_next_site(session, run_id, "a") is not None
        assert await claim_next_site(session, run_id, "a") is None

    async def test_concurrent_workers_never_claim_the_same_site(self, engine, session):
        """SKIP LOCKED is what makes the hand-out atomic across workers."""
        run_id = await _seed_run(session, 6)
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

        async def worker(name: str) -> list[str]:
            claimed = []
            async with maker() as own_session:
                while True:
                    claim = await claim_next_site(own_session, run_id, name)
                    if claim is None:
                        return claimed
                    claimed.append(claim[0])

        results = await asyncio.gather(*(worker(f"agent-{i}") for i in range(4)))
        all_claimed = [site_run_id for batch in results for site_run_id in batch]
        assert len(all_claimed) == 6
        assert len(set(all_claimed)) == 6  # no double-claim

    async def test_cancel_clears_only_pending_work(self, session):
        run_id = await _seed_run(session, 4)
        await claim_next_site(session, run_id, "agent-0")  # one now running
        cancelled = await cancel_pending(session, run_id)
        assert cancelled == 3
        assert await pending_count(session, run_id) == 0

        running = (
            await session.execute(
                select(SiteRun).where(
                    SiteRun.run_id == run_id, SiteRun.status == SiteRunStatus.RUNNING
                )
            )
        ).scalars().all()
        assert len(running) == 1  # in-flight site is left to finish its step


class TestResume:
    async def test_running_sites_return_to_the_queue(self, session):
        run_id = await _seed_run(session, 3)
        await claim_next_site(session, run_id, "agent-0")
        await claim_next_site(session, run_id, "agent-1")
        assert await pending_count(session, run_id) == 1

        # The server is stopped and restarted here.
        requeued = await reset_running_for_resume(session, run_id)
        assert requeued == 2
        assert await pending_count(session, run_id) == 3

    async def test_completed_sites_are_not_reprocessed(self, session):
        run_id = await _seed_run(session, 3)
        claim = await claim_next_site(session, run_id, "agent-0")
        site_run = await session.get(SiteRun, claim[0])
        site_run.status = SiteRunStatus.COMPLETED
        site_run.records_found = 12
        await session.commit()

        await reset_running_for_resume(session, run_id)
        assert await pending_count(session, run_id) == 2

        await session.refresh(site_run)
        assert site_run.status == SiteRunStatus.COMPLETED
        assert site_run.records_found == 12  # collected data survives


class TestHardStops:
    async def test_record_limit_trips_and_is_reported_as_stopped_at_limit(self):
        limits = RunLimits(max_records=10)
        await limits.add_records(6)
        assert not limits.should_stop
        await limits.add_records(5)
        assert limits.should_stop
        assert limits.stop_reason is StopReason.MAX_RECORDS
        assert limits.stopped_at_limit  # not "completed", not "failed"

    async def test_spend_limit_trips(self):
        limits = RunLimits(max_spend_usd=0.50)
        await limits.add_usage(100_000, 10_000, 0.20)
        assert not limits.should_stop
        await limits.add_usage(1_000_000, 100_000, 0.40)
        assert limits.stop_reason is StopReason.MAX_SPEND

    async def test_spend_is_metered_live_not_at_the_end(self):
        limits = RunLimits()
        await limits.add_usage(1_000, 500, 0.001)
        await limits.add_usage(2_000, 800, 0.002)
        snapshot = limits.snapshot()
        assert snapshot["tokens_in"] == 3_000
        assert snapshot["tokens_out"] == 1_300
        assert snapshot["spend_usd"] == pytest.approx(0.003)

    async def test_no_limits_means_never_stopping(self):
        limits = RunLimits()
        await limits.add_records(1_000_000)
        await limits.add_usage(10**9, 10**9, 10_000.0)
        assert not limits.should_stop

    async def test_cancellation_is_distinct_from_a_limit(self):
        limits = RunLimits(max_records=100)
        limits.cancel()
        assert limits.should_stop
        assert limits.stop_reason is StopReason.CANCELLED
        assert not limits.stopped_at_limit

    async def test_counters_are_safe_under_concurrent_workers(self):
        limits = RunLimits()
        await asyncio.gather(*(limits.add_records(1) for _ in range(200)))
        assert limits.records_collected == 200


class TestMemoryCeiling:
    def test_reasonable_concurrency_is_allowed(self):
        report = check_memory_ceiling(1)
        assert report["required_mb"] > 0

    def test_above_the_configured_context_cap_is_refused(self):
        with pytest.raises(MemoryCeilingExceeded) as exc:
            check_memory_ceiling(99)
        assert "MAX_CONCURRENT_CONTEXTS" in str(exc.value)

    def test_the_error_says_what_would_fit(self, monkeypatch):
        # Fail loudly before a run starts rather than thrashing the box.
        from agentscrape.orchestrator import limits as limits_module

        monkeypatch.setattr(
            limits_module.settings, "estimated_mb_per_context", 10**6
        )
        with pytest.raises(MemoryCeilingExceeded) as exc:
            check_memory_ceiling(8)
        assert "concurrency" in str(exc.value).lower()
