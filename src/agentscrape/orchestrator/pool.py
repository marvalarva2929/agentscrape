"""Worker pool: reusable agents, each owning one isolated browser context.

Requirements this satisfies (Section 9):
  * concurrency is per run, 1-8, and the pool size is respected
  * a site's failure, hang or crash affects only that site
  * agents are reused: finishing a site takes the next one off the queue
  * cancellation stops new work immediately and leaves data consistent
  * everything is bounded: per-site budget, per-page timeout, run timeout
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from ..browser.fetcher import Fetcher
from ..browser.renderer import BrowserPool
from ..config import settings
from ..db.enums import RunStatus, SiteRunStatus, StopReason
from ..db.models import Run, SiteRun
from ..db.session import get_sessionmaker
from ..llm.usage import Usage
from ..pipeline.runner import run_site
from .events import EventEmitter, EventType, get_event_bus
from .limits import RunLimits, SiteCounts, check_memory_ceiling
from .queue import claim_next_site, heartbeat, reset_running_for_resume

log = logging.getLogger("agentscrape.pool")

HEARTBEAT_SECONDS = 2.0


class RunOrchestrator:
    """Runs one Run to completion (or to a hard stop)."""

    def __init__(
        self,
        run_id: str,
        *,
        concurrency: int,
        step_budget: int,
        limits: RunLimits,
        use_browser: bool = True,
        crawl_strategy: str | None = None,
        modes: list[str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.crawl_strategy = crawl_strategy
        self.modes = modes
        self.concurrency = max(1, min(concurrency, settings.max_concurrency))
        self.step_budget = step_budget
        self.limits = limits
        self.use_browser = use_browser
        self.emitter = EventEmitter(run_id)
        self.sessionmaker = get_sessionmaker()
        self._browser: BrowserPool | None = None
        self._workers: list[asyncio.Task] = []
        self._heartbeat_task: asyncio.Task | None = None
        self._active_sites: dict[str, str] = {}
        # Held so the background shutdown started by `cancel` is not collected.
        self._stopping: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Run until the queue drains, a limit trips, or cancellation."""
        async with self.sessionmaker() as session:
            sites = await session.scalar(select(Run.sites_total).where(Run.id == self.run_id))
        # An agent per site at most: a one-school run launched from the UI
        # opened four browser contexts and used one, so a handful of such runs
        # side by side hit the memory ceiling.
        if sites:
            self.concurrency = max(1, min(self.concurrency, int(sites)))

        # Fail before doing any work rather than thrashing the box.
        memory = check_memory_ceiling(self.concurrency if self.use_browser else 1)
        log.info("memory check passed for concurrency %d: %s", self.concurrency, memory)

        async with self.sessionmaker() as session:
            resumed = await reset_running_for_resume(session, self.run_id)
            await session.execute(
                update(Run)
                .where(Run.id == self.run_id)
                .values(
                    status=RunStatus.RUNNING,
                    started_at=func.coalesce(Run.started_at, datetime.now(UTC)),
                    heartbeat_at=datetime.now(UTC),
                )
            )
            await session.commit()
        if resumed:
            log.info("resumed run %s: %d sites returned to the queue", self.run_id, resumed)

        await self.emitter.emit(
            EventType.RUN_STARTED,
            concurrency=self.concurrency, resumed_sites=resumed,
            **self.limits.snapshot(),
        )

        if self.use_browser:
            self._browser = BrowserPool(size=self.concurrency)
            await self._browser.start()

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            async with Fetcher(concurrency=settings.in_site_fetch_concurrency) as fetcher:
                self._workers = [
                    asyncio.create_task(self._worker(f"agent-{i}", fetcher))
                    for i in range(self.concurrency)
                ]
                await asyncio.wait_for(
                    asyncio.gather(*self._workers, return_exceptions=True),
                    timeout=settings.run_timeout_seconds,
                )
        except TimeoutError:
            log.warning("run %s exceeded its overall timeout", self.run_id)
            self.limits.stop_reason = StopReason.RUN_TIMEOUT
            await self._stop_workers()
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            if self._browser is not None:
                await self._browser.stop()

        await self._finish()

    async def cancel(self) -> None:
        """Stop taking new work now; in-flight sites finish their current step.

        Returns as soon as the intent is recorded. Unwinding the workers means
        waiting for whatever each in-flight site is inside — a model call can
        hold for `llm_timeout_seconds` — so a caller that waited for it would
        block far longer than a browser is willing to wait, and the person who
        pressed stop would be told the crawl could not be stopped when it had
        been. `limits.cancel()` is what actually stops the run: workers claim
        no further school, and `_finish` records it as cancelled.
        """
        self.limits.cancel()
        if self._stopping is None or self._stopping.done():
            self._stopping = asyncio.create_task(self._stop_workers())

    async def _stop_workers(self) -> None:
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    # -- workers -----------------------------------------------------------

    async def _worker(self, agent_id: str, fetcher: Fetcher) -> None:
        """Claim, run, repeat. The agent and its context are reused throughout."""
        context = None
        if self._browser is not None:
            context = await self._browser.acquire(agent_id)
        await self.emitter.emit(EventType.AGENT_SPAWNED, agent_id=agent_id)

        try:
            # A count limit ends the crawl: claim no further school.
            while not (self.limits.should_stop or self.limits.crawl_limit_reached):
                async with self.sessionmaker() as session:
                    claim = await claim_next_site(session, self.run_id, agent_id)
                if claim is None:
                    break

                site_run_id, site_id, url, budget = claim
                self._active_sites[agent_id] = site_run_id

                meter_hook = self._usage_hook()
                try:
                    state = await run_site(
                        site_id=site_id,
                        site_run_id=site_run_id,
                        root_url=url,
                        run_id=self.run_id,
                        agent_id=agent_id,
                        step_budget=budget or self.step_budget,
                        browser_context=context,
                        emitter=self.emitter,
                        should_stop=lambda: self.limits.should_stop,
                        crawl_limit_reached=lambda: self.limits.crawl_limit_reached,
                        on_counts=self._counts_hook(site_run_id),
                        on_usage=meter_hook,
                        fetcher=fetcher,
                        crawl_strategy=self.crawl_strategy,
                        modes=self.modes,
                    )
                except asyncio.CancelledError:
                    await self._mark_cancelled(site_run_id)
                    raise
                except Exception as exc:
                    # run_site already isolates failures; this is belt and braces
                    # so a worker can never die and strand its pool slot. The
                    # school is failed and counted, not left `running` forever.
                    log.exception("worker %s: unhandled error on %s", agent_id, url)
                    await self._mark_site_failed(site_run_id, exc)
                    continue
                finally:
                    self._active_sites.pop(agent_id, None)

                await self._absorb(state, meter_hook)

        except asyncio.CancelledError:
            log.info("worker %s cancelled", agent_id)
        finally:
            if self._browser is not None:
                # Keep the context for reuse; only drop it if the browser is going.
                pass
            await self.emitter.emit(EventType.AGENT_RETIRED, agent_id=agent_id)

    def _usage_hook(self):
        async def hook(usage: Usage) -> None:
            await self.limits.add_usage(
                usage.input_tokens, usage.output_tokens, usage.cost_usd
            )

        return hook

    def _counts_hook(self, site_run_id: str):
        async def hook(counts: SiteCounts) -> None:
            await self.limits.report_counts(site_run_id, counts)
            # So the queue and Past crawls show people while the school is still
            # being read, not only once it finishes.
            try:
                async with self.sessionmaker() as session:
                    await session.execute(
                        update(SiteRun)
                        .where(SiteRun.id == site_run_id)
                        .values(records_found=counts.people)
                    )
                    await session.commit()
            except Exception:
                log.debug("could not save live counts for %s", site_run_id, exc_info=True)

        return hook

    async def _absorb(self, state, meter_hook) -> None:
        """Fold a finished site's numbers into the run's live counters."""
        # A school that crawled already reported its unique counts per batch;
        # one that never reached extraction (skipped) did not.
        if not self.limits.has_reported(state.get("site_run_id", "")):
            await self.limits.add_records(len(set(state.get("seen_record_ids") or [])))

        await self._persist_totals()

        if self.limits.should_stop or self.limits.crawl_limit_reached:
            # Winding down, not finished: other schools or a directory search
            # may still be running. The stream ends on the terminal event that
            # `_finish` sends, so this is only a progress update.
            await self.emitter.emit(EventType.RUN_PROGRESS, **self.limits.snapshot())

    async def _persist_totals(self, *, beat: bool = False) -> None:
        """Write the run's counters, worked out from its schools and its meter.

        Totals are recomputed from the school rows rather than incremented, so
        retrying a school or resuming a run after a restart cannot count it
        twice. Spend and tokens come from the meter, which was seeded from the
        row when the run started, so they only ever grow.
        """
        async with self.sessionmaker() as session:
            def _count(status):
                return func.count(SiteRun.id).filter(SiteRun.status == status)

            row = (
                await session.execute(
                    select(
                        _count(SiteRunStatus.COMPLETED),
                        _count(SiteRunStatus.SKIPPED),
                        _count(SiteRunStatus.FAILED),
                        _count(SiteRunStatus.REJECTED),
                        func.coalesce(func.sum(SiteRun.records_found), 0),
                        func.coalesce(func.sum(SiteRun.records_new), 0),
                        func.coalesce(func.sum(SiteRun.records_changed), 0),
                        func.coalesce(func.sum(SiteRun.records_missing), 0),
                    ).where(SiteRun.run_id == self.run_id)
                )
            ).one()
            values = {
                "sites_completed": row[0],
                "sites_skipped": row[1],
                "sites_failed": row[2],
                "sites_rejected": row[3],
                "records_found": int(row[4]),
                "records_new": int(row[5]),
                "records_changed": int(row[6]),
                "records_missing": int(row[7]),
                "tokens_in": self.limits.tokens_in,
                "tokens_out": self.limits.tokens_out,
                "spend_usd": self.limits.spend_usd,
            }
            if beat:
                values["heartbeat_at"] = datetime.now(UTC)
            await session.execute(update(Run).where(Run.id == self.run_id).values(**values))
            await session.commit()

    async def _mark_site_failed(self, site_run_id: str, exc: Exception) -> None:
        async with self.sessionmaker() as session:
            await session.execute(
                update(SiteRun)
                .where(SiteRun.id == site_run_id, SiteRun.status == SiteRunStatus.RUNNING)
                .values(
                    status=SiteRunStatus.FAILED,
                    error_code="worker_error",
                    error_message=f"{type(exc).__name__}: {exc}"[:500],
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()
        await self._persist_totals()

    async def _mark_cancelled(self, site_run_id: str) -> None:
        async with self.sessionmaker() as session:
            await session.execute(
                update(SiteRun)
                .where(SiteRun.id == site_run_id)
                .values(status=SiteRunStatus.CANCELLED, finished_at=datetime.now(UTC))
            )
            await session.commit()

    # -- heartbeat ---------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Authoritative aggregate progress every couple of seconds.

        Individual events are lost across reconnects, so the frontend treats
        this as the source of truth for its counters. It is also what saves the
        run's spend and people to the database while the crawl is going, and
        what tells the queue this run still has a process behind it, so one bad
        iteration is logged and skipped instead of ending the loop for good.
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                try:
                    await self._beat()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("run %s: heartbeat failed; will try again", self.run_id)
        except asyncio.CancelledError:
            pass

    async def _beat(self) -> None:
        async with self.sessionmaker() as session:
            for site_run_id in list(self._active_sites.values()):
                await heartbeat(session, site_run_id)
        await self._persist_totals(beat=True)

        async with self.sessionmaker() as session:
            run = await session.get(Run, self.run_id)
            if run is None:
                return
            payload = {
                "sites_total": run.sites_total,
                "sites_completed": run.sites_completed,
                "sites_skipped": run.sites_skipped,
                "sites_failed": run.sites_failed,
                "sites_rejected": run.sites_rejected,
                "records_found": run.records_found,
                "records_new": run.records_new,
                "records_changed": run.records_changed,
                "records_missing": run.records_missing,
                "active_agents": len(self._active_sites),
            }
        _, agents, _ = get_event_bus().snapshot(self.run_id)
        await self.emitter.emit(
            EventType.HEARTBEAT, **payload, **self.limits.snapshot(), agents=agents
        )

    # -- completion --------------------------------------------------------

    async def _finish(self) -> None:
        # Last word on the counters, from the schools as they ended.
        await self._persist_totals()
        if self.limits.stopped_at_limit:
            status = RunStatus.STOPPED_AT_LIMIT
            event = EventType.RUN_STOPPED_AT_LIMIT
        elif self.limits.cancelled:
            status = RunStatus.CANCELLED
            event = EventType.RUN_CANCELLED
        elif self.limits.stop_reason == StopReason.RUN_TIMEOUT:
            status = RunStatus.STOPPED_AT_LIMIT
            event = EventType.RUN_STOPPED_AT_LIMIT
        else:
            status = RunStatus.COMPLETED
            event = EventType.RUN_COMPLETED

        async with self.sessionmaker() as session:
            run = await session.get(Run, self.run_id)
            directory_failure = await session.scalar(select(SiteRun.error_message).where(
                SiteRun.run_id == self.run_id,
                SiteRun.error_code.startswith("DIRECTORY_"),
            ).limit(1))
            if status == RunStatus.COMPLETED and directory_failure:
                status = RunStatus.FAILED
                event = EventType.RUN_FAILED
            if run is not None:
                run.error_message = directory_failure
            await session.execute(
                update(Run)
                .where(Run.id == self.run_id)
                .values(
                    status=status,
                    error_message=directory_failure,
                    stop_reason=self.limits.stop_reason,
                    finished_at=datetime.now(UTC),
                    tokens_in=self.limits.tokens_in,
                    tokens_out=self.limits.tokens_out,
                    spend_usd=self.limits.spend_usd,
                )
            )
            await session.commit()

        await self.emitter.emit(event, status=str(status), error=directory_failure, **self.limits.snapshot())
        log.info("run %s finished: %s (%s)", self.run_id, status, self.limits.snapshot())


# Registry of live orchestrators, so cancel can reach a running run.
_active: dict[str, RunOrchestrator] = {}


def register(orchestrator: RunOrchestrator) -> None:
    _active[orchestrator.run_id] = orchestrator


def unregister(run_id: str) -> None:
    _active.pop(run_id, None)


def get_active(run_id: str) -> RunOrchestrator | None:
    return _active.get(run_id)


# Queued runs that are not crawls (a verification pass) hold the queue the same
# way, but have no orchestrator: just the task doing the work, so cancel can stop it.
_active_tasks: dict[str, asyncio.Task] = {}


def register_task(run_id: str, task: asyncio.Task) -> None:
    _active_tasks[run_id] = task


def unregister_task(run_id: str) -> None:
    _active_tasks.pop(run_id, None)


def get_active_task(run_id: str) -> asyncio.Task | None:
    return _active_tasks.get(run_id)


def active_run_ids() -> list[str]:
    return list(_active) + list(_active_tasks)
