"""On-demand verification of already-scraped role labels.

Triggered manually ("Verify these rows" in the UI) against data already in
the database - never automatically as part of a crawl, and never re-crawls
anything beyond the one stored source page per record. Re-fetches each
record's current source page with a plain HTTP GET (no browser render: if a
page needed one to show its people, that already happened during the crawl
that produced it) and asks the model which roles the page text supports.

The job id comes back immediately; the client polls for status.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..browser.fetcher import Fetcher
from ..db.enums import VerificationStatus
from ..db.models import Record, RecordVersion, VerificationJob
from ..db.session import session_scope
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


async def create_verification_job(
    session: AsyncSession, body: VerificationCreate
) -> VerificationJob:
    if not body.site_id and not body.record_ids:
        raise ValueError("verification needs a site_id or a list of record_ids")
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


async def run_verification(job_id: str) -> None:
    """Check every target record's source page. Failures are recorded on the
    job, never raised, so a job that dies partway still shows what it got."""
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
        semaphore = asyncio.Semaphore(_PAGE_CONCURRENCY)
        meter = UsageMeter()

        async def _one(url: str, title: str | None, people: list[RoleCheckInput]) -> None:
            nonlocal checked, corrected
            async with semaphore:
                result = await fetcher.get(url)
            if not result.ok or not result.is_html:
                return
            text = html_to_model_text(result.text)
            roles_by_record = await verify_page_roles(
                url=url, title=title or "", text=text, people=people, meter=meter,
            )
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

        async with Fetcher() as fetcher:
            await asyncio.gather(*(
                _one(url, title, people) for (url, title), people in by_page.items()
            ))

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
