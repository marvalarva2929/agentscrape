"""Full-pipeline runs against a local fixture institution.

Covers what unit tests cannot: discovery feeding ranking feeding extraction
feeding reconciliation, then a second run exercising known paths and the skip
check, and a run that stops at a hard limit.
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select

from agentscrape.config import settings
from agentscrape.db.enums import RecordStatus, RunStatus, SiteRunStatus
from agentscrape.db.models import KnownPath, Record, Run, Site, SiteRun
from agentscrape.domain.schemas import RunConfigIn, RunCreate
from agentscrape.orchestrator.limits import RunLimits
from agentscrape.orchestrator.pool import RunOrchestrator
from agentscrape.orchestrator.service import create_run
from agentscrape.pipeline.checkpoint import CHECKPOINT_VERSION

from .fixture_server import serve


@pytest_asyncio.fixture(autouse=True)
def fast_and_offline(monkeypatch):
    """Keep these tests local and quick.

    crt.sh is disabled (there is no certificate transparency for 127.0.0.1) and
    the rate limiter is loosened, since politeness is not the property under
    test here and it is covered separately.
    """
    monkeypatch.setattr(settings, "enable_crt_sh", False)
    monkeypatch.setattr(settings, "requests_per_second_per_domain", 50.0)
    monkeypatch.setattr(settings, "discovery_timeout_seconds", 30)
    monkeypatch.setattr(settings, "discovery_source_timeout_seconds", 10)
    from agentscrape.browser import ratelimit

    monkeypatch.setattr(ratelimit, "_limiter", None)
    yield


async def _run_once(
    site_urls, *, concurrency=2, step_budget=20, limits=None, session=None,
    crawl_strategy=None, modes=None,
):
    body = RunCreate(
        sites=list(site_urls),
        config=RunConfigIn(
            concurrency=concurrency, step_budget=step_budget,
            skip_threshold=0.90,
            max_records=getattr(limits, "max_records", None) if limits else None,
            crawl_strategy=crawl_strategy,
            modes=modes or ["crawl"],
        ),
    )
    run = await create_run(session, body)
    orchestrator = RunOrchestrator(
        run.id,
        concurrency=concurrency,
        skip_threshold=0.90,
        step_budget=step_budget,
        limits=limits or RunLimits(),
        use_browser=False,  # the fixture site is static; no escalation needed
        crawl_strategy=crawl_strategy,
        modes=modes,
    )
    await orchestrator.start()
    return run.id


class TestSingleSiteEndToEnd:
    async def test_extracts_reconciles_and_records_provenance(self, session):
        # The agent strategy reads every page, including the one-person alumni
        # page the hybrid gate leaves unread (see test_hybrid).
        with serve() as site:
            run_id = await _run_once([site.base], session=session, crawl_strategy="agent")

        run = await session.get(Run, run_id)
        await session.refresh(run)
        assert run.status == RunStatus.COMPLETED

        records = (await session.execute(select(Record))).scalars().all()
        by_name = {r.full_name: r for r in records}

        # Residents from the table, fellow from the cards.
        assert "Ann Riley" in by_name
        assert "Cara Diaz" in by_name
        assert "Dana Fields" in by_name

        # Scope inverted: everyone on the site is collected and labelled.
        assert by_name["Owen Grant"].category == "faculty"
        assert by_name["Owen Grant"].position == "Program Director"
        # Alumni pages are a source now; their people are labelled alumni.
        assert by_name["Gone Person"].category == "alumni"

        ann = by_name["Ann Riley"]
        assert ann.email == "ann.riley@example.edu"
        assert ann.category == "resident"
        assert ann.pgy_at_capture == 1
        assert ann.status == RecordStatus.NEW
        assert ann.current_version_id is not None

        versions = await session.execute(
            select(Record.id).where(Record.current_version_id.isnot(None))
        )
        assert len(list(versions.scalars().all())) == len(records)

    async def test_productive_paths_are_remembered(self, session):
        with serve() as site:
            await _run_once([site.base], session=session)

        paths = (await session.execute(select(KnownPath))).scalars().all()
        urls = {p.url for p in paths}
        assert any(u.endswith("/residents") for u in urls)
        # A page that yielded nobody is never promoted.
        assert not any(u.endswith("/news") for u in urls)
        residents = next(p for p in paths if p.url.endswith("/residents"))
        assert residents.success_count == 1
        assert residents.avg_records >= 3
        assert residents.is_active


class TestSecondRun:
    async def test_unchanged_site_is_skipped(self, session):
        with serve() as site:
            await _run_once([site.base], session=session)
            run_id = await _run_once([site.base], session=session)

        site_run = (
            await session.execute(select(SiteRun).where(SiteRun.run_id == run_id))
        ).scalar_one()
        assert site_run.status == SiteRunStatus.SKIPPED
        assert site_run.skip_reason is not None
        # A skipped site does no extraction work at all.
        assert site_run.steps_taken == 0

    async def test_changed_roster_is_rescanned_and_versioned(self, session):
        with serve() as site:
            await _run_once([site.base], session=session)

            # Ben leaves, Ann is promoted a year, a new resident arrives.
            site.residents = [
                ("Ann Riley", 2, "ann.riley@example.edu"),
                ("Cara Diaz", 3, "cara.diaz@example.edu"),
                ("Dev Shah", 1, "dev.shah@example.edu"),
            ]
            run_id = await _run_once([site.base], session=session)

        site_run = (
            await session.execute(select(SiteRun).where(SiteRun.run_id == run_id))
        ).scalar_one()
        assert site_run.status == SiteRunStatus.COMPLETED

        records = {
            r.full_name: r for r in (await session.execute(select(Record))).scalars().all()
        }
        # Nothing is ever deleted.
        assert "Ben Cole" in records
        assert records["Ben Cole"].status == RecordStatus.MISSING
        assert records["Ben Cole"].missing_since is not None

        assert records["Dev Shah"].status == RecordStatus.NEW

        ann = records["Ann Riley"]
        assert ann.pgy_at_capture == 2
        assert ann.status == RecordStatus.CHANGED
        assert ann.version_count == 2

        from agentscrape.db.models import RecordVersion

        latest = (
            await session.execute(
                select(RecordVersion)
                .where(RecordVersion.record_id == ann.id)
                .order_by(RecordVersion.version_no.desc())
            )
        ).scalars().first()
        assert latest.changed_fields["pgy_at_capture"] == {"from": 1, "to": 2}

    async def test_force_rescan_overrides_the_skip(self, session):
        with serve() as site:
            await _run_once([site.base], session=session)

            body = RunCreate(
                sites=[site.base],
                config=RunConfigIn(concurrency=1, step_budget=20, force_rescan=True),
            )
            run = await create_run(session, body)
            orchestrator = RunOrchestrator(
                run.id, concurrency=1, skip_threshold=0.90, step_budget=20,
                limits=RunLimits(), use_browser=False,
            )
            await orchestrator.start()

        site_run = (
            await session.execute(select(SiteRun).where(SiteRun.run_id == run.id))
        ).scalar_one()
        assert site_run.status == SiteRunStatus.COMPLETED
        assert site_run.steps_taken > 0


class TestConcurrency:
    async def test_two_sites_run_without_corrupting_each_other(self, session):
        with serve("hospital-a.localhost") as site_a, serve("hospital-b.localhost") as site_b:
            run_id = await _run_once(
                [site_a.base, site_b.base], concurrency=2, session=session
            )

        run = await session.get(Run, run_id)
        await session.refresh(run)
        assert run.status == RunStatus.COMPLETED
        assert run.sites_completed == 2

        sites = (await session.execute(select(Site))).scalars().all()
        assert len(sites) == 2
        for site in sites:
            records = (
                await session.execute(select(Record).where(Record.site_id == site.id))
            ).scalars().all()
            # Each site gets its own copy; identity is scoped per site.
            assert len(records) >= 3

    async def test_a_dead_site_does_not_affect_a_healthy_one(self, session):
        with serve("healthy.localhost") as site:
            # Nothing listens on this port, so the site cannot be reached at all.
            run_id = await _run_once(
                [site.base, "http://dead.localhost:9"], concurrency=2, session=session
            )

        run = await session.get(Run, run_id)
        await session.refresh(run)
        # The run finishes; the healthy site still produced records.
        assert run.status == RunStatus.COMPLETED
        assert run.records_found >= 3

        statuses = {
            sr.status
            for sr in (
                await session.execute(select(SiteRun).where(SiteRun.run_id == run_id))
            ).scalars().all()
        }
        assert SiteRunStatus.COMPLETED in statuses
        assert statuses & {SiteRunStatus.REJECTED, SiteRunStatus.FAILED}


class TestHardStops:
    async def test_record_limit_stops_the_run_and_keeps_partial_results(self, session):
        with serve("limit-a.localhost") as site_a, serve("limit-b.localhost") as site_b:
            limits = RunLimits(max_records=1)
            run_id = await _run_once(
                [site_a.base, site_b.base], concurrency=1, limits=limits, session=session
            )

        run = await session.get(Run, run_id)
        await session.refresh(run)
        assert run.status == RunStatus.STOPPED_AT_LIMIT
        assert run.stop_reason == "max_records"

        # A stopped run's partial results are valid results.
        records = (await session.execute(select(Record))).scalars().all()
        assert len(records) >= 1
        assert all(r.current_version_id for r in records)


    async def test_trainee_limit_stops_the_crawl_mid_school(self, session):
        """Counts are reported after every batch, so a limit trips while the
        school is still being crawled, and the next school is never claimed."""
        from agentscrape.db.models import SiteRun

        with serve("rf-a.localhost") as site_a, serve("rf-b.localhost") as site_b:
            limits = RunLimits(max_trainees=1)
            run_id = await _run_once(
                [site_a.base, site_b.base], concurrency=1, limits=limits,
                step_budget=1, session=session,
            )

        run = await session.get(Run, run_id)
        await session.refresh(run)
        assert run.status == RunStatus.STOPPED_AT_LIMIT
        assert run.stop_reason == "max_trainees"
        statuses = sorted(
            (await session.execute(select(SiteRun.status).where(SiteRun.run_id == run_id))).scalars()
        )
        assert "pending" in statuses  # the second school was never started


class TestDuplicateInputs:
    async def test_the_same_institution_twice_is_collapsed(self, session):
        """A CSV listing a site with and without www must not fail the run."""
        with serve() as site:
            body = RunCreate(
                sites=[site.base, f"{site.base}/", f"{site.base}/index.html"],
                config=RunConfigIn(concurrency=1, step_budget=5),
            )
            run = await create_run(session, body)

        assert run.sites_total == 1
        site_runs = (
            await session.execute(select(SiteRun).where(SiteRun.run_id == run.id))
        ).scalars().all()
        assert len(site_runs) == 1


class TestResume:
    """The server is stopped on demand, so a site can be interrupted anywhere."""

    async def test_interrupted_site_resumes_without_rediscovering(self, session):
        from agentscrape.db.models import SiteRunVisit
        from agentscrape.pipeline.nodes import discover as discover_module

        with serve("resume.localhost") as site:
            # First attempt: stop the run the moment one batch is checkpointed.
            body = RunCreate(
                sites=[site.base],
                config=RunConfigIn(concurrency=1, step_budget=2),
            )
            run = await create_run(session, body)
            limits = RunLimits()
            orchestrator = RunOrchestrator(
                run.id, concurrency=1, skip_threshold=0.90, step_budget=2,
                limits=limits, use_browser=False,
            )
            await orchestrator.start()

            site_run = (
                await session.execute(select(SiteRun).where(SiteRun.run_id == run.id))
            ).scalar_one()
            await session.refresh(site_run)
            first_visits = (
                await session.execute(
                    select(SiteRunVisit).where(SiteRunVisit.site_run_id == site_run.id)
                )
            ).scalars().all()
            assert first_visits, "the first attempt should have visited something"

            # Put the site back in the queue as a restart would, and make
            # discovery fail loudly if it is reached again.
            checkpoint = {
                "version": CHECKPOINT_VERSION,
                "candidates": [
                    {"url": f"{site.base}/residents", "score": 9.0, "is_known_path": False},
                    {"url": f"{site.base}/fellows", "score": 9.0, "is_known_path": False},
                ],
                "cursor": 0, "steps_taken": 0, "candidates_considered": 5,
                "known_path_hits": 0, "records_new": 0, "records_changed": 0,
                "records_unchanged": 0, "seen_record_ids": [], "fingerprint": {},
                "dominant_specialty": None, "similarity_score": None,
            }
            site_run.status = SiteRunStatus.PENDING
            site_run.checkpoint_state = checkpoint
            site_run.step_budget = 20
            await session.commit()

            called = {"discover": False}
            original = discover_module.discover_links

            async def fail_if_called(state, deps):
                called["discover"] = True
                return await original(state, deps)

            discover_module.discover_links = fail_if_called
            try:
                resumed = RunOrchestrator(
                    run.id, concurrency=1, skip_threshold=0.90, step_budget=20,
                    limits=RunLimits(), use_browser=False,
                )
                await resumed.start()
            finally:
                discover_module.discover_links = original

        await session.refresh(site_run)
        assert site_run.status == SiteRunStatus.COMPLETED
        # The whole point: discovery is the expensive stage and must be skipped.
        assert called["discover"] is False

        records = (await session.execute(select(Record))).scalars().all()
        assert {r.full_name for r in records} >= {"Ann Riley", "Cara Diaz"}

        # A finished site must not keep a checkpoint that could resurrect it.
        assert site_run.checkpoint_state is None

    async def test_completed_work_survives_the_interruption(self, session):
        with serve("survive.localhost") as site:
            body = RunCreate(
                sites=[site.base], config=RunConfigIn(concurrency=1, step_budget=2)
            )
            run = await create_run(session, body)
            orchestrator = RunOrchestrator(
                run.id, concurrency=1, skip_threshold=0.90, step_budget=2,
                limits=RunLimits(), use_browser=False,
            )
            await orchestrator.start()

        # Reconciliation runs per batch, so whatever was collected is persisted
        # even though the site never finished its candidate list.
        records = (await session.execute(select(Record))).scalars().all()
        assert records
        assert all(r.current_version_id for r in records)


class TestDeadEntryLink:
    async def test_a_dead_entry_page_falls_back_to_the_home_page(self, session):
        # The school sheet's hub links rot: Arizona's and UVA's returned 404.
        with serve(hostname="deadlink.localhost") as site:
            await _run_once([f"{site.base}/no-such-hub"], session=session)

        names = {r.full_name for r in (await session.execute(select(Record))).scalars()}
        assert {"Ann Riley", "Cara Diaz"} <= names
