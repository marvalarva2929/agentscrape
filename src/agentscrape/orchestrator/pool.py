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

from sqlalchemy import update

from ..browser.fetcher import Fetcher
from ..browser.renderer import BrowserPool
from ..config import settings
from ..db.enums import RunStatus, SiteRunStatus, StopReason
from ..db.models import CsvSubmission, Run, SiteRun
from ..db.session import get_sessionmaker
from ..llm.usage import Usage
from ..pipeline.runner import run_site
from .events import EventEmitter, EventType
from .limits import RunLimits, check_memory_ceiling
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
        skip_threshold: float,
        step_budget: int,
        limits: RunLimits,
        use_browser: bool = True,
    ) -> None:
        self.run_id = run_id
        self.concurrency = max(1, min(concurrency, settings.max_concurrency))
        self.skip_threshold = skip_threshold
        self.step_budget = step_budget
        self.limits = limits
        self.use_browser = use_browser
        self.emitter = EventEmitter(run_id)
        self.sessionmaker = get_sessionmaker()
        self._browser: BrowserPool | None = None
        self._workers: list[asyncio.Task] = []
        self._heartbeat_task: asyncio.Task | None = None
        self._active_sites: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Run until the queue drains, a limit trips, or cancellation."""
        # Fail before doing any work rather than thrashing the box.
        memory = check_memory_ceiling(self.concurrency if self.use_browser else 1)
        log.info("memory check passed for concurrency %d: %s", self.concurrency, memory)

        async with self.sessionmaker() as session:
            resumed = await reset_running_for_resume(session, self.run_id)
            await session.execute(
                update(Run)
                .where(Run.id == self.run_id)
                .values(status=RunStatus.RUNNING, started_at=datetime.now(UTC))
            )
            await session.execute(
                update(CsvSubmission)
                .where(CsvSubmission.run_id == self.run_id)
                .values(status="done", reviewed_at=datetime.now(UTC))
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
        """Stop taking new work now; in-flight sites finish their current step."""
        self.limits.cancel()
        await self._stop_workers()

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
            while not self.limits.should_stop:
                async with self.sessionmaker() as session:
                    claim = await claim_next_site(session, self.run_id, agent_id)
                if claim is None:
                    break

                site_run_id, site_id, url, force_rescan, budget = claim
                self._active_sites[agent_id] = site_run_id

                meter_hook = self._usage_hook()
                try:
                    state = await run_site(
                        site_id=site_id,
                        site_run_id=site_run_id,
                        root_url=url,
                        run_id=self.run_id,
                        agent_id=agent_id,
                        force_rescan=force_rescan,
                        skip_threshold=self.skip_threshold,
                        step_budget=budget or self.step_budget,
                        browser_context=context,
                        emitter=self.emitter,
                        should_stop=lambda: self.limits.should_stop,
                        fetcher=fetcher,
                        on_usage=meter_hook,
                    )
                except asyncio.CancelledError:
                    await self._mark_cancelled(site_run_id)
                    raise
                except Exception:
                    # run_site already isolates failures; this is belt and braces
                    # so a worker can never die and strand its pool slot.
                    log.exception("worker %s: unhandled error on %s", agent_id, url)
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
            # Persist the live aggregate immediately. Site-level totals are
            # still written at finalization, but a monitor must not wait for a
            # long crawl to finish before seeing its spend.
            async with self.sessionmaker() as session:
                await session.execute(
                    update(Run)
                    .where(Run.id == self.run_id)
                    .values(
                        tokens_in=self.limits.tokens_in,
                        tokens_out=self.limits.tokens_out,
                        spend_usd=self.limits.spend_usd,
                    )
                )
                await session.commit()
            await self.emitter.emit(EventType.RUN_PROGRESS, **self.limits.snapshot())

        return hook

    async def _absorb(self, state, meter_hook) -> None:
        """Fold a finished site's numbers into the run's live counters."""
        found = (
            state.get("records_new", 0)
            + state.get("records_changed", 0)
            + state.get("records_unchanged", 0)
        )
        await self.limits.add_records(found)

        status = str(state.get("status") or "")
        if status in ("skipped", str(SiteRunStatus.SKIPPED)):
            site_column = "sites_skipped"
        elif status in ("failed", str(SiteRunStatus.FAILED)):
            site_column = "sites_failed"
        elif status in ("rejected", str(SiteRunStatus.REJECTED)):
            site_column = "sites_rejected"
        else:
            site_column = "sites_completed"

        # Atomic column arithmetic, not read-modify-write. Several workers finish
        # concurrently, and reading the Run into Python to increment it loses
        # updates: two sites completing at once would count as one.
        increments = {
            "records_found": Run.records_found + found,
            "records_new": Run.records_new + state.get("records_new", 0),
            "records_changed": Run.records_changed + state.get("records_changed", 0),
            "records_missing": Run.records_missing + state.get("records_missing", 0),
            site_column: getattr(Run, site_column) + 1,
            # These come from the shared limits object, which is already the
            # authoritative running total, so they are assignments not deltas.
            "tokens_in": self.limits.tokens_in,
            "tokens_out": self.limits.tokens_out,
            "spend_usd": self.limits.spend_usd,
        }
        async with self.sessionmaker() as session:
            await session.execute(
                update(Run).where(Run.id == self.run_id).values(**increments)
            )
            await session.commit()

        if self.limits.should_stop:
            await self.emitter.emit(
                EventType.RUN_STOPPED_AT_LIMIT
                if self.limits.stopped_at_limit
                else EventType.RUN_CANCELLED,
                **self.limits.snapshot(),
            )

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
        this as the source of truth for its counters.
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                async with self.sessionmaker() as session:
                    run = await session.get(Run, self.run_id)
                    if run is None:
                        continue
                    for site_run_id in list(self._active_sites.values()):
                        await heartbeat(session, site_run_id)
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
                await self.emitter.emit(
                    EventType.HEARTBEAT, **payload, **self.limits.snapshot()
                )
        except asyncio.CancelledError:
            pass

    # -- completion --------------------------------------------------------

    async def _finish(self) -> None:
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
            await session.execute(
                update(Run)
                .where(Run.id == self.run_id)
                .values(
                    status=status,
                    stop_reason=self.limits.stop_reason,
                    finished_at=datetime.now(UTC),
                    tokens_in=self.limits.tokens_in,
                    tokens_out=self.limits.tokens_out,
                    spend_usd=self.limits.spend_usd,
                )
            )
            await session.commit()

        await self.emitter.emit(event, status=str(status), **self.limits.snapshot())
        log.info("run %s finished: %s (%s)", self.run_id, status, self.limits.snapshot())


# Registry of live orchestrators, so cancel can reach a running run.
_active: dict[str, RunOrchestrator] = {}


def register(orchestrator: RunOrchestrator) -> None:
    _active[orchestrator.run_id] = orchestrator


def unregister(run_id: str) -> None:
    _active.pop(run_id, None)


def get_active(run_id: str) -> RunOrchestrator | None:
    return _active.get(run_id)


def active_run_ids() -> list[str]:
    return list(_active)
