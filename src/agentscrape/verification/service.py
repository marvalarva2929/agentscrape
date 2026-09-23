"""On-demand verification of already-scraped role labels.

Triggered manually ("Verify these rows" in the UI) against data already in
the database - never automatically as part of a crawl, and never re-crawls
anything beyond the one stored source page per record. Re-fetches each
record's current source page with a plain HTTP GET and asks the model which
roles the page text supports; `run_verification(..., use_browser=True)`
escalates to a real browser render for a page that grounds nobody as plain
HTML, the same escalation the crawler itself uses for client-rendered pages.

The job id comes back immediately; the client polls for status.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..browser.fetcher import Fetcher
from ..browser.renderer import BrowserPool, render_page
from ..db.enums import ExtractionMethod, RecordStatus, SiteRunStatus, VerificationStatus
from ..db.ids import version_id as new_version_id
from ..db.models import Record, RecordVersion, SiteRun, VerificationJob
from ..db.session import session_scope
from ..domain.matching import VERSIONED_FIELDS
from ..domain.schemas import VerificationCreate
from ..extraction.text import html_to_model_text
from ..llm.usage import UsageMeter
from ..llm.verify import RoleCheckInput, verify_page_roles

log = logging.getLogger("agentscrape.verification")

# Background tasks are kept in a module-level set for the same reason exports
# are: asyncio holds only a weak reference to a running task, so a
# fire-and-forget job could otherwise be garbage collected mid-run.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# Distinct source pages fetched and read at once. Bounded independently of the
# crawl's own concurrency, since this always runs well after a crawl.
_PAGE_CONCURRENCY = 8

# A verification job in either of these states still holds the site.
_ACTIVE_VERIFICATION = (VerificationStatus.PENDING, VerificationStatus.RUNNING)


class VerificationBusy(ValueError):
    """A crawl or another verification already has this site; try again once
    it finishes rather than run two passes over the same records at once."""


async def _site_busy(session: AsyncSession, site_id: str) -> str | None:
    """Why a verification can't start for this site right now, or None."""
    crawling = await session.scalar(
        select(SiteRun.id)
        .where(SiteRun.site_id == site_id, SiteRun.status == SiteRunStatus.RUNNING)
        .limit(1)
    )
    if crawling is not None:
        return "a crawl is still running for this site"
    verifying = await session.scalar(
        select(VerificationJob.id)
        .where(
            VerificationJob.site_id == site_id,
            VerificationJob.status.in_(_ACTIVE_VERIFICATION),
        )
        .limit(1)
    )
    if verifying is not None:
        return "a verification is already running for this site"
    return None


async def create_verification_job(
    session: AsyncSession, body: VerificationCreate
) -> VerificationJob:
    """Persist the job and kick off checking in the background.

    Refuses to start while the site is still crawling, or another
    verification for it is already in flight - one pass over a site's
    records at a time, so two runs never race writing the same `roles` field.
    """
    if not body.site_id and not body.record_ids:
        raise ValueError("verification needs a site_id or a list of record_ids")

    if body.site_id:
        site_ids = {body.site_id}
    else:
        site_ids = set(
            (
                await session.execute(
                    select(Record.site_id).where(Record.id.in_(body.record_ids)).distinct()
                )
            ).scalars()
        )
    for site_id in site_ids:
        reason = await _site_busy(session, site_id)
        if reason:
            raise VerificationBusy(f"cannot start verification for {site_id}: {reason}")

    job = VerificationJob(site_id=body.site_id, record_ids=body.record_ids or None)
    session.add(job)
    await session.flush()
    await session.commit()
    _spawn(run_verification(job.id))
    return job


async def _targets(session: AsyncSession, job: VerificationJob) -> list:
    """(record_id, full_name, category, position, source_url, page_title) for
    every record this job covers that has a current source page."""
    statement = (
        select(
            Record.id, Record.full_name, Record.category, Record.position,
            RecordVersion.source_url, RecordVersion.page_title,
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
        current_version = (
            await session.get(RecordVersion, record.current_version_id)
            if record.current_version_id else None
        )
        record.category = new_category
        fields = {field: getattr(record, field) for field in VERSIONED_FIELDS}
        record.version_count += 1
        record.last_changed_at = datetime.now(UTC)
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
        promoted.append(
            {"record_id": record.id, "full_name": record.full_name,
             "from": previous, "to": new_category}
        )

    await session.commit()
    return {"promoted": len(promoted), "details": promoted}


async def run_verification(
    job_id: str,
    *,
    concurrency: int = _PAGE_CONCURRENCY,
    page_attempts: int = 1,
    use_browser: bool = False,
) -> None:
    """Check every target record's source page. Failures are recorded on the
    job, never raised, so a job that dies partway still shows what it got.

    `page_attempts` re-tries a page that came back with nothing (fetch
    failure, or a model call that never returned usable JSON) before giving
    up on it - useful for a mop-up pass over records an earlier, more
    concurrent run missed, where a gentler pace recovers pages that failed
    under load rather than because they are actually unreachable.

    `use_browser` renders a page in a real browser when the plain HTTP fetch
    grounds nobody on it - the same escalation the crawler itself uses for a
    page whose people are painted in by client-side script (tabs,
    "load more", a roster loaded from an API) rather than present in the raw
    HTML. Off by default: it is much slower and every record checked this
    way still only re-confirms the page already on file.
    """
    try:
        async with session_scope() as session:
            job = await session.get(VerificationJob, job_id)
            if job is None:
                return
            rows = await _targets(session, job)
            await session.execute(
                update(VerificationJob)
                .where(VerificationJob.id == job_id)
                .values(status=VerificationStatus.RUNNING, records_total=len(rows))
            )

        by_page: dict[tuple[str, str | None], list[RoleCheckInput]] = {}
        for record_id, full_name, category, position, source_url, page_title in rows:
            by_page.setdefault((source_url, page_title), []).append(
                RoleCheckInput(
                    record_id=record_id, full_name=full_name,
                    category=category, position=position,
                )
            )

        checked = 0
        corrected = 0
        rendered_count = 0
        semaphore = asyncio.Semaphore(concurrency)
        meter = UsageMeter()
        browser_pool: BrowserPool | None = None
        browser_context = None
        if use_browser:
            browser_pool = BrowserPool(size=1)
            await browser_pool.start()
            browser_context = await browser_pool.acquire("verification")

        async def _one(url: str, title: str | None, people: list[RoleCheckInput]) -> None:
            nonlocal checked, corrected, rendered_count
            # Held for the whole page - fetch, model call, and any render
            # escalation together - so `concurrency` bounds total concurrent
            # model calls too, not just the I/O either side of them. A
            # semaphore only around the fetch still let every page's model
            # call fire at once, which is what was overloading the provider.
            async with semaphore:
                roles_by_record: dict[str, list[str]] = {}
                for attempt in range(page_attempts):
                    result = await fetcher.get(url)
                    # A plain-text source (a program's .txt intro doc, not a
                    # web page) has no markup to strip - html_to_model_text
                    # would otherwise discard it as unparseable HTML and skip
                    # straight to giving up, without even trying the browser
                    # fallback.
                    is_plain_text = "text/plain" in result.content_type
                    if not result.ok or not (result.is_html or is_plain_text):
                        if attempt + 1 < page_attempts:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        return
                    text = result.text if is_plain_text else html_to_model_text(result.text)
                    roles_by_record = await verify_page_roles(
                        url=url, title=title or "", text=text, people=people, meter=meter,
                    )
                    if roles_by_record or attempt + 1 == page_attempts:
                        break
                    await asyncio.sleep(1.5 * (attempt + 1))
                if not roles_by_record and browser_context is not None:
                    # Plain HTML grounded nobody: the page may paint its
                    # people in with client-side script, same as the crawler
                    # sees. Retried too - a render is expensive enough that
                    # losing its model call to a transient rate limit, with
                    # nothing left to fall back on, would waste the render.
                    for render_attempt in range(page_attempts):
                        rendered = await render_page(browser_context, url)
                        if not (rendered.ok and rendered.html):
                            break
                        rendered_count += 1
                        text = html_to_model_text(rendered.html)
                        roles_by_record = await verify_page_roles(
                            url=url, title=title or rendered.title, text=text,
                            people=people, meter=meter,
                        )
                        if roles_by_record or render_attempt + 1 == page_attempts:
                            break
                        await asyncio.sleep(1.5 * (render_attempt + 1))
            if not roles_by_record:
                return
            prior = {p.record_id: p.category for p in people}
            now = datetime.now(UTC)
            async with session_scope() as write_session:
                for record_id, roles in roles_by_record.items():
                    record = await write_session.get(Record, record_id)
                    if record is None:
                        continue
                    checked += 1
                    record.roles = roles
                    record.roles_checked_at = now
                    if roles != [prior.get(record_id)]:
                        corrected += 1

        try:
            async with Fetcher() as fetcher:
                await asyncio.gather(*(
                    _one(url, title, people) for (url, title), people in by_page.items()
                ))
        finally:
            if browser_pool is not None:
                await browser_pool.stop()
        log.info("verification %s: %d pages escalated to a browser render", job_id, rendered_count)

        async with session_scope() as session:
            await session.execute(
                update(VerificationJob)
                .where(VerificationJob.id == job_id)
                .values(
                    status=VerificationStatus.COMPLETED,
                    records_checked=checked,
                    records_corrected=corrected,
                    finished_at=datetime.now(UTC),
                )
            )
        log.info(
            "verification %s: %d/%d records checked, %d corrected",
            job_id, checked, len(rows), corrected,
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
