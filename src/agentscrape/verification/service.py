"""Verification of already-scraped role labels, run as an item in the queue.

Started by the "Verify" action in the UI, and automatically once a crawl
finishes a school. Either way it never starts on its own: it becomes a
queued run of kind `verify` and waits its turn behind whatever crawl holds
the model budget, exactly like a crawl. When its turn comes it re-reads each
record's stored source page and asks the model which roles the page supports.

A page is read with a plain HTTP GET first. When that is turned away (many
hospital sites refuse a script - more so from a cloud server's address than
from a laptop) or the crawl itself needed a browser to read that page, it is
opened in a real browser instead, the same escalation the crawler uses.

The job id comes back immediately; the client polls it for progress.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..browser.fetcher import Fetcher
from ..browser.renderer import BrowserPool, render_page
from ..db.enums import (
    RUN_KIND_VERIFY,
    TERMINAL_RUN_STATUSES,
    ExtractionMethod,
    FetchMode,
    RecordStatus,
    RunStatus,
    VerificationStatus,
)
from ..db.ids import version_id as new_version_id
from ..db.models import Record, RecordVersion, Run, Site, VerificationJob
from ..db.session import session_scope
from ..domain.matching import VERSIONED_FIELDS
from ..domain.schemas import VerificationCreate
from ..extraction.text import html_to_model_text
from ..llm.usage import UsageMeter
from ..llm.verify import RoleCheckInput, verify_page_roles

log = logging.getLogger("agentscrape.verification")

# Distinct source pages fetched and read at once.
_PAGE_CONCURRENCY = 8
# Browser renders at once: each is a real page in Chromium, far heavier than a GET.
_RENDER_CONCURRENCY = 2

# How often a running verification tells the queue it is still alive. Well
# inside the scheduler's RUN_STALE_SECONDS.
_HEARTBEAT_SECONDS = 5.0

# A verification job in either of these states has not finished.
_ACTIVE_VERIFICATION = (VerificationStatus.PENDING, VerificationStatus.RUNNING)


async def _label(session: AsyncSession, body: VerificationCreate) -> str:
    """What the queue calls this pass: how much it checks, and where."""
    if body.site_id:
        site_ids = [body.site_id]
    else:
        site_ids = list(
            (
                await session.execute(
                    select(Record.site_id).where(Record.id.in_(body.record_ids or [])).distinct()
                )
            ).scalars()
        )
    names = [
        name or hospital or domain
        for name, hospital, domain in (
            await session.execute(
                select(Site.name, Site.hospital_name, Site.root_domain)
                .where(Site.id.in_(site_ids))
                .order_by(Site.name)
            )
        ).all()
    ]
    where = names[0] if names else "unknown school"
    if len(names) > 1:
        where = f"{where} + {len(names) - 1} more"
    if body.site_id:
        return f"Verify all people — {where}"
    count = len(body.record_ids or [])
    return f"Verify {count} row{'' if count == 1 else 's'} — {where}"


async def create_verification_job(
    session: AsyncSession, body: VerificationCreate
) -> VerificationJob:
    """Put a verification pass in the queue. Does not start it.

    It waits behind every run already queued, crawls included, so it never
    shares the model budget with one. A whole-site pass for a site that
    already has one waiting is not queued twice: the waiting one is returned.
    """
    if not body.site_id and not body.record_ids:
        raise ValueError("verification needs a site_id or a list of record_ids")

    if body.site_id and not body.record_ids:
        waiting = await session.scalar(
            select(VerificationJob)
            .where(
                VerificationJob.site_id == body.site_id,
                VerificationJob.record_ids.is_(None),
                VerificationJob.status == VerificationStatus.PENDING,
                VerificationJob.run_id.isnot(None),
            )
            .limit(1)
        )
        if waiting is not None:
            return waiting

    from ..orchestrator.scheduler import next_rank

    run = Run(
        kind=RUN_KIND_VERIFY,
        status=RunStatus.PENDING,
        label=await _label(session, body),
        config={"queued": True, "site_id": body.site_id, "records": len(body.record_ids or [])},
        queued=True,
        queue_rank=await next_rank(session),
        sites_total=0,
    )
    session.add(run)
    await session.flush()
    job = VerificationJob(
        site_id=body.site_id, record_ids=body.record_ids or None, run_id=run.id,
    )
    session.add(job)
    await session.flush()
    await session.commit()
    log.info("queued verification %s as run %s (%s)", job.id, run.id, run.label)
    return job


async def end_unstarted_job(session: AsyncSession, run_id: str, reason: str) -> None:
    """Fail the job of a verify run that will never run. Caller commits."""
    await session.execute(
        update(VerificationJob)
        .where(
            VerificationJob.run_id == run_id,
            VerificationJob.status.in_(_ACTIVE_VERIFICATION),
        )
        .values(status=VerificationStatus.FAILED, error=reason, finished_at=datetime.now(UTC))
    )


async def recover_orphaned_verification_jobs() -> int:
    """Fail every unfinished job that nothing will ever pick up again.

    A job whose queued run is still live is left alone: the queue puts a run
    abandoned by a restart back in line and runs it again from the start.
    What is failed here is a job from before verification was queued (no
    run), or one whose run already ended without finishing it - left at
    `running` forever, it would read as a verification that never ends.
    """
    async with session_scope() as session:
        ended_runs = select(Run.id).where(Run.status.in_(TERMINAL_RUN_STATUSES))
        result = await session.execute(
            update(VerificationJob)
            .where(
                VerificationJob.status.in_(_ACTIVE_VERIFICATION),
                VerificationJob.run_id.is_(None) | VerificationJob.run_id.in_(ended_runs),
            )
            .values(
                status=VerificationStatus.FAILED,
                error="orphaned: the server restarted while this job was running",
                finished_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        count = int(result.rowcount or 0)
    if count:
        log.warning("recovered %d verification job(s) orphaned by a restart", count)
    return count


# -- the queued run ---------------------------------------------------------


async def launch_verification_run(run_id: str) -> None:
    """Start a verify run the queue just claimed, as a background task.

    Registered with the pool like an orchestrator, so the queue sees it as
    running and nothing else starts until it hands the budget on.
    """
    from ..orchestrator.pool import register_task

    async with session_scope() as session:
        job_id = await session.scalar(
            select(VerificationJob.id).where(VerificationJob.run_id == run_id).limit(1)
        )
    task = asyncio.create_task(_run_queued(run_id, job_id))
    register_task(run_id, task)


async def _heartbeat(run_id: str, meter: UsageMeter) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        try:
            async with session_scope() as session:
                await session.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        heartbeat_at=datetime.now(UTC),
                        tokens_in=meter.total.input_tokens,
                        tokens_out=meter.total.output_tokens,
                        spend_usd=meter.cost_usd,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("verify run %s: heartbeat failed; will try again", run_id)


async def _run_queued(run_id: str, job_id: str | None) -> None:
    """Run one verification job as the queue's current run, then hand on."""
    from ..orchestrator.pool import unregister_task
    from ..orchestrator.scheduler import on_run_finished

    meter = UsageMeter(scope=f"verify:{run_id}")
    beat = asyncio.create_task(_heartbeat(run_id, meter))
    status, error = RunStatus.FAILED, None
    try:
        if job_id is None:
            error = "this queued verification has no job to run"
        else:
            await run_verification(job_id, meter=meter)
            async with session_scope() as session:
                job = await session.get(VerificationJob, job_id)
            if job is not None and job.status == VerificationStatus.COMPLETED:
                status = RunStatus.COMPLETED
            else:
                error = job.error if job is not None else "the verification job disappeared"
    except asyncio.CancelledError:
        status, error = RunStatus.CANCELLED, "stopped before it finished"
        if job_id is not None:
            async with session_scope() as session:
                await session.execute(
                    update(VerificationJob)
                    .where(
                        VerificationJob.id == job_id,
                        VerificationJob.status.in_(_ACTIVE_VERIFICATION),
                    )
                    .values(
                        status=VerificationStatus.FAILED,
                        error="stopped before it finished",
                        finished_at=datetime.now(UTC),
                    )
                )
    except Exception as exc:
        log.exception("verify run %s crashed", run_id)
        error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        beat.cancel()
        try:
            async with session_scope() as session:
                await session.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        status=status,
                        error_message=error,
                        finished_at=datetime.now(UTC),
                        tokens_in=meter.total.input_tokens,
                        tokens_out=meter.total.output_tokens,
                        spend_usd=meter.cost_usd,
                    )
                )
        except Exception:
            log.exception("verify run %s: could not record how it ended", run_id)
        unregister_task(run_id)
        # Only now is the model budget free for the next run in line.
        await on_run_finished(run_id)


# -- the check itself -------------------------------------------------------


async def _targets(session: AsyncSession, job: VerificationJob) -> list:
    """(record_id, full_name, category, position, source_url, page_title,
    fetch_mode) for every record this job covers that has a current source page."""
    statement = (
        select(
            Record.id, Record.full_name, Record.category, Record.position,
            RecordVersion.source_url, RecordVersion.page_title, RecordVersion.fetch_mode,
        )
        .join(RecordVersion, Record.current_version_id == RecordVersion.id)
        .where(Record.full_name.isnot(None), RecordVersion.source_url.isnot(None))
    )
    if job.record_ids:
        statement = statement.where(Record.id.in_(job.record_ids))
    else:
        statement = statement.where(Record.site_id == job.site_id)
    return (await session.execute(statement)).all()


async def promote_verified_roles(session: AsyncSession, *, site_id: str) -> dict:
    """Write a verification job's unambiguous findings into `category`.

    Only promotes records whose confirmed `roles` is a single value that
    disagrees with the stored `category` - a real mislabel, not the "also
    holds a second role" additions, which have no single correct value to
    write back (those stay visible in `roles` for whoever reads the record).
    Writes a new RecordVersion so the change carries the same provenance as
    any other: `extraction_method` is `verify`, the source page is whichever
    one is already on file.
    """
    rows = (
        await session.execute(
            select(Record).where(Record.site_id == site_id, Record.roles.isnot(None))
        )
    ).scalars().all()

    promoted: list[dict] = []
    for record in rows:
        roles = record.roles or []
        if len(roles) != 1 or roles[0] == record.category:
            continue
        previous = record.category
        new_category = roles[0]
        await _promote_record(session, record, new_category)
        promoted.append(
            {"record_id": record.id, "full_name": record.full_name,
             "from": previous, "to": new_category}
        )

    await session.commit()
    return {"promoted": len(promoted), "details": promoted}


async def _promote_record(session: AsyncSession, record: Record, new_category: str) -> None:
    """Apply a single confirmed role in the same transaction as verification."""
    previous = record.category
    current_version = (
        await session.get(RecordVersion, record.current_version_id)
        if record.current_version_id else None
    )
    record.category = new_category
    fields = {field: getattr(record, field) for field in VERSIONED_FIELDS}
    record.version_count += 1
    record.last_changed_at = datetime.now(UTC)
    if record.status != RecordStatus.MISSING:
        record.status = RecordStatus.CHANGED
    version = RecordVersion(
        id=new_version_id(),
        record_id=record.id,
        version_no=record.version_count,
        fields=fields,
        changed_fields={"category": {"from": previous, "to": new_category}},
        source_url=current_version.source_url if current_version else "",
        page_title=current_version.page_title if current_version else None,
        captured_at=datetime.now(UTC),
        extraction_method=ExtractionMethod.VERIFY,
        fetch_mode=current_version.fetch_mode if current_version else "html",
        confidence=record.confidence,
    )
    session.add(version)
    await session.flush()
    record.current_version_id = version.id


class _Browser:
    """A browser started the first time a page needs one, and only then.

    Most pages read fine over plain HTTP, and a verification pass that never
    needs Chromium should not pay to launch it. If it cannot start at all
    (no Chromium on this machine), every later request gets the same error
    rather than trying again per page.
    """

    def __init__(self) -> None:
        self._pool: BrowserPool | None = None
        self._context = None
        self._error: str | None = None
        self._lock = asyncio.Lock()
        self._renders = asyncio.Semaphore(_RENDER_CONCURRENCY)
        self.rendered = 0

    async def _ensure(self):
        async with self._lock:
            if self._context is None and self._error is None:
                try:
                    self._pool = BrowserPool(size=1)
                    await self._pool.start()
                    self._context = await self._pool.acquire("verification")
                except Exception as exc:
                    log.exception("verification: could not start a browser")
                    self._error = f"no browser available ({type(exc).__name__}: {exc})"[:200]
            return self._context

    async def read(self, url: str) -> tuple[str | None, str, str | None]:
        """(model text, page title, error) for the page as a browser shows it."""
        context = await self._ensure()
        if context is None:
            return None, "", self._error
        async with self._renders:
            rendered = await render_page(context, url)
        if not (rendered.ok and rendered.html):
            return None, "", f"browser: {rendered.error or 'empty page'}"
        self.rendered += 1
        return html_to_model_text(rendered.html), rendered.title, None

    async def stop(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.stop()
            except Exception:
                log.exception("verification: could not stop the browser")


async def _read_plain(fetcher: Fetcher, url: str) -> tuple[str | None, str | None]:
    """(model text, error) for the page over plain HTTP."""
    result = await fetcher.get(url)
    # A plain-text source (a program's .txt intro doc, not a web page) has no
    # markup to strip; html_to_model_text would discard it as unparseable HTML.
    is_plain_text = "text/plain" in result.content_type
    if not result.ok:
        return None, result.error or f"HTTP {result.status}"
    if not (result.is_html or is_plain_text):
        return None, f"not a web page ({result.content_type or 'unknown type'})"
    return (result.text if is_plain_text else html_to_model_text(result.text)), None


def _summarize(errors: Counter) -> str:
    return ", ".join(f"{reason} ×{count}" for reason, count in errors.most_common(3))


async def run_verification(
    job_id: str,
    *,
    concurrency: int = _PAGE_CONCURRENCY,
    page_attempts: int = 1,
    use_browser: bool = False,
    meter: UsageMeter | None = None,
) -> None:
    """Check every target record's source page. Failures are recorded on the
    job, never raised, so a job that dies partway still shows what it got.

    Progress is written as each page finishes, so a client polling a long
    pass sees it move rather than a zero until the very end.

    A page is opened in a browser whenever plain HTTP cannot read it, or the
    crawl itself needed a browser for it. `use_browser` additionally renders
    a page that plain HTTP read but that grounded nobody - off by default:
    it is much slower and rarely changes the answer.

    `page_attempts` re-tries a page that came back with nothing before
    giving up on it.
    """
    meter = meter or UsageMeter(scope=f"verify:{job_id}")
    try:
        async with session_scope() as session:
            job = await session.get(VerificationJob, job_id)
            if job is None:
                return
            rows = await _targets(session, job)
            # From zero every time: a pass the queue re-runs after a restart
            # starts over rather than adding to the counts of the one it replaces.
            await session.execute(
                update(VerificationJob)
                .where(VerificationJob.id == job_id)
                .values(
                    status=VerificationStatus.RUNNING, records_total=len(rows),
                    records_checked=0, records_corrected=0, error=None, finished_at=None,
                )
            )

        by_page: dict[tuple[str, str | None], list[RoleCheckInput]] = {}
        needs_browser: set[str] = set()
        for record_id, full_name, category, position, source_url, page_title, mode in rows:
            by_page.setdefault((source_url, page_title), []).append(
                RoleCheckInput(
                    record_id=record_id, full_name=full_name,
                    category=category, position=position,
                )
            )
            if mode in (FetchMode.RENDER, FetchMode.BOTH):
                needs_browser.add(source_url)

        checked = 0
        corrected = 0
        fetch_failures = 0
        model_failures = 0
        fetch_errors: Counter = Counter()
        semaphore = asyncio.Semaphore(concurrency)
        browser = _Browser()

        async def _ask(url: str, title: str, text: str, people: list[RoleCheckInput]) -> dict[str, str]:
            for attempt in range(page_attempts):
                roles = await verify_page_roles(
                    url=url, title=title, text=text, people=people, meter=meter,
                )
                if roles or attempt + 1 == page_attempts:
                    return roles
                await asyncio.sleep(1.5 * (attempt + 1))
            return {}

        async def _one(url: str, title: str | None, people: list[RoleCheckInput]) -> None:
            nonlocal checked, corrected, fetch_failures, model_failures
            # Held for the whole page - fetch, model call and any render - so
            # `concurrency` bounds concurrent model calls too, not just I/O.
            async with semaphore:
                roles_by_record: dict[str, str] = {}
                text, page_title, error = None, title or "", None
                if url not in needs_browser:
                    text, error = await _read_plain(fetcher, url)
                    if text is not None:
                        roles_by_record = await _ask(url, page_title, text, people)
                # Plain HTTP was refused, or the crawl only ever read this page
                # in a browser, or (with use_browser) plain HTML grounded nobody.
                if text is None or (use_browser and not roles_by_record):
                    rendered, rendered_title, render_error = await browser.read(url)
                    if rendered is not None:
                        text = rendered
                        roles_by_record = await _ask(
                            url, title or rendered_title, rendered, people,
                        )
                    elif text is None:
                        error = f"{error}; {render_error}" if error else render_error
                if text is None:
                    fetch_failures += len(people)
                    fetch_errors[error or "unreadable"] += 1
                    log.info("verification %s: could not read %s: %s", job_id, url, error)
                    return
                if not roles_by_record:
                    # Read, but the model never gave an answer that grounded
                    # anyone - not the same as "nothing needed correcting".
                    model_failures += len(people)
                    return
            prior = {p.record_id: p.category for p in people}
            now = datetime.now(UTC)
            page_checked = page_corrected = 0
            async with session_scope() as write_session:
                for record_id, role in roles_by_record.items():
                    record = await write_session.get(Record, record_id, with_for_update=True)
                    if record is None:
                        continue
                    page_checked += 1
                    # `roles` remains an API-compatible stored field, but a
                    # verified record now has exactly one canonical role.
                    record.roles = [role]
                    record.roles_checked_at = now
                    if role != record.category:
                        await _promote_record(write_session, record, role)
                    if role != prior.get(record_id):
                        page_corrected += 1
                # Added in SQL, so pages finishing out of order can't write
                # an older total over a newer one.
                await write_session.execute(
                    update(VerificationJob)
                    .where(VerificationJob.id == job_id)
                    .values(
                        records_checked=VerificationJob.records_checked + page_checked,
                        records_corrected=VerificationJob.records_corrected + page_corrected,
                        updated_at=func.now(),
                    )
                )
            checked += page_checked
            corrected += page_corrected

        try:
            async with Fetcher() as fetcher:
                await asyncio.gather(*(
                    _one(url, title, people) for (url, title), people in by_page.items()
                ))
        finally:
            await browser.stop()

        failed = len(rows) - checked
        reasons = []
        if fetch_failures:
            reasons.append(f"{fetch_failures} couldn't be read ({_summarize(fetch_errors)})")
        if model_failures:
            why = f"; last error: {meter.last_failure}" if meter.last_failure else ""
            reasons.append(f"{model_failures} got no usable answer from the model{why}")
        summary = "; ".join(reasons)

        # A pass that checked nobody is the checker never running, not a
        # clean bill of health: report it as a failure with the reason.
        total_failure = checked == 0 and bool(rows)
        async with session_scope() as session:
            await session.execute(
                update(VerificationJob)
                .where(VerificationJob.id == job_id)
                .values(
                    status=VerificationStatus.FAILED if total_failure else VerificationStatus.COMPLETED,
                    records_checked=checked,
                    records_corrected=corrected,
                    error=(
                        f"checked 0 of {len(rows)} records: {summary or 'no page could be checked'}"[:500]
                        if total_failure
                        else (f"{failed} of {len(rows)} not checked: {summary}"[:500] if failed and summary else None)
                    ),
                    finished_at=datetime.now(UTC),
                )
            )
        log.info(
            "verification %s: %d/%d records checked, %d corrected, %d pages rendered (%s)",
            job_id, checked, len(rows), corrected, browser.rendered, summary or "no failures",
        )
    except Exception as exc:
        log.exception("verification %s failed", job_id)
        async with session_scope() as session:
            await session.execute(
                update(VerificationJob)
                .where(VerificationJob.id == job_id)
                .values(
                    status=VerificationStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                    finished_at=datetime.now(UTC),
                )
            )
