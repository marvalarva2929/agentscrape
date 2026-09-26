"""Verification of already-scraped role labels, run as an item in the queue.

Started by the "Verify" action in the UI, and automatically once a crawl
finishes a school. Either way it becomes a queued run of kind `verify` and
waits its turn behind whatever crawl holds the model budget. When its turn
comes it re-reads each record's stored source page, first applying a
conservative deterministic current-role check; it asks the model only for
ambiguous evidence.

A page is read with a plain HTTP GET first. When that is turned away (many
hospital sites refuse a script - more so from a cloud server's address than
from a laptop) or the crawl itself needed a browser to read that page, it is
opened in a real browser instead, the same escalation the crawler uses.

The job id comes back immediately; the client polls it for progress.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..browser.fetcher import Fetcher
from ..browser.renderer import BrowserPool, render_page
from ..db.enums import (
    RUN_KIND_VERIFY,
    TERMINAL_RUN_STATUSES,
    ExtractionMethod,
    FetchMode,
    PersonCategory,
    RecordStatus,
    RecordVerificationOutcome,
    RunStatus,
    VerificationStatus,
)
from ..db.ids import version_id as new_version_id
from ..db.models import Record, RecordVersion, Run, Site, VerificationAttempt, VerificationJob
from ..db.session import session_scope
from ..domain.matching import VERSIONED_FIELDS
from ..domain.schemas import VerificationCreate
from ..extraction.text import html_to_model_text
from ..llm.role_evidence import (
    has_direct_current_trainee_evidence,
    is_governance_or_non_gme_context,
)
from ..llm.usage import UsageMeter
from ..llm.verify import RoleCheckInput, RoleDecision, verify_page_roles

log = logging.getLogger("agentscrape.verification")

# Distinct source pages fetched and read at once.
_PAGE_CONCURRENCY = 8
# Browser renders at once: each is a real page in Chromium, far heavier than a GET.
_RENDER_CONCURRENCY = 2

# How often a running verification tells the queue it is still alive. Well
# inside the scheduler's RUN_STALE_SECONDS.
_HEARTBEAT_SECONDS = 5.0
# This clock is deliberately created in run_verification, after the queued
# worker has actually begun.  Crawl, directory work, and queue waiting never
# consume this budget.
_VERIFICATION_LIMIT = timedelta(hours=4)

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


# The only outcomes a resume should treat as already settled. Null (never
# attempted) and every other outcome (insufficient evidence, source
# unavailable, verification error) remain eligible for another attempt.
_CONFIRMED_OUTCOMES = (
    RecordVerificationOutcome.VERIFIED_RESIDENT,
    RecordVerificationOutcome.VERIFIED_FELLOW,
    RecordVerificationOutcome.VERIFIED_NON_TRAINEE,
)


async def resume_verification_job(session: AsyncSession, previous_job_id: str) -> VerificationJob:
    """Continue a stopped job, re-checking only records with no confirmed
    outcome yet (never attempted, insufficient evidence, unavailable source,
    or a verification error).

    Per-record verification state lives on the record, so a resume remains safe
    across a process restart and does not discard already captured evidence.
    """
    previous = await session.get(VerificationJob, previous_job_id)
    if previous is None:
        raise ValueError(f"No verification job with id {previous_job_id!r}.")
    rows = await _targets(session, previous)
    target_ids = [row[0] for row in rows]
    confirmed = set(
        await session.scalars(
            select(Record.id).where(
                Record.id.in_(target_ids),
                Record.verification_outcome.in_(_CONFIRMED_OUTCOMES),
            )
        )
    )
    pending_ids = [record_id for record_id in target_ids if record_id not in confirmed]
    if not pending_ids:
        raise ValueError("Every record in that verification is already proven.")
    return await create_verification_job(session, VerificationCreate(record_ids=pending_ids))


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


def _verification_quality(
    *, role: str, evidence: str, position: str | None, url: str, title: str,
    duplicate_name: bool = False,
) -> tuple[float, str, str]:
    """Deterministic final gate: a model cannot certify a weak source."""
    page = f"{url} {title}".casefold()
    article = any(token in page for token in ("/news", "/blog", "/article", "press-release", "spotlight", "award"))
    position_text = (position or "").casefold()
    conflict = any(token in position_text for token in ("professor", "attending", "faculty", "director", "coordinator", "alumni", "former"))
    if duplicate_name:
        return 0.15, "high", "Multiple records at this school share this name; identity must be disambiguated before verification."
    if role in ("resident", "fellow") and conflict:
        return 0.20, "high", "Printed position conflicts with a current trainee role; deeper verification required."
    if role in ("resident", "fellow") and article:
        return 0.35, "high", "Source looks like an article or announcement, not a canonical roster; deeper verification required."
    if role in ("resident", "fellow") and _SUSPICIOUS_CONTEXT.search(evidence):
        return 0.25, "high", "Evidence is historical, article-like, or otherwise does not establish a current trainee role."
    if role == "resident" and not _CURRENT_ROLE.search(evidence):
        return 0.30, "high", "Evidence does not explicitly establish a current resident role."
    if role == "fellow" and not re.search(r"\b(current\s+)?fellows?\b", evidence, re.IGNORECASE):
        return 0.30, "high", "Evidence does not explicitly establish a current fellow role."
    if role in ("resident", "fellow"):
        return 0.96, "verified", "Exact name and current-training evidence were found on an institution source."
    if role == "unknown":
        return 0.30, "needs_review", "The page names this person but does not establish a current role."
    return 0.90, "verified", "Exact name and role evidence were found on the source page."


async def _audit_attempt(job_id: str, people: list[RoleCheckInput], *, stage: str, outcome: str, url: str, detail: str | None = None) -> None:
    """Persist enough context to reproduce a failed or risky decision later."""
    matched = re.search(r"HTTP (\d{3})", detail or "")
    async with session_scope() as session:
        session.add_all(
            VerificationAttempt(job_id=job_id, record_id=p.record_id, stage=stage, outcome=outcome, source_url=url, final_url=url, http_status=int(matched.group(1)) if matched else None, detail=(detail or None)[:500] if detail else None)
            for p in people
        )


async def _write_outcome(record_ids: list[str], outcome: RecordVerificationOutcome) -> None:
    """Stamp every targeted record's verification_outcome for this attempt.

    Called for every record this run reaches, whatever the result, so a
    completed job's outcome counts always sum to the number of records it
    actually processed - nobody disappears into an unexplained remainder.
    """
    if not record_ids:
        return
    async with session_scope() as session:
        await session.execute(
            update(Record).where(Record.id.in_(record_ids)).values(verification_outcome=outcome)
        )


_TRAINEE_CATEGORIES = (str(PersonCategory.RESIDENT), str(PersonCategory.FELLOW))


async def _downgrade_ungrounded_trainees(record_ids: list[str]) -> int:
    """A stored resident/fellow claim verification could not ground is not
    left standing merely because nothing better came along: false positives
    are worse than an unknown role. Downgrades to "unknown" - the same
    fallback crawl-time extraction now uses for the identical reason - with
    the same versioned provenance (`_promote_record`) as any other
    correction. The completed correction is `verified_non_trainee`: that enum
    deliberately includes an explicit unknown role where the source does not
    support a current GME claim. Previously this helper changed the category
    but left the preliminary `insufficient_evidence` outcome in place, making
    a successful correction look like a verification failure in the UI.

    Only ever touches records currently labelled resident/fellow; every other
    category is left to the ordinary INSUFFICIENT_EVIDENCE outcome. Returns
    how many records were actually changed.
    """
    if not record_ids:
        return 0
    changed = 0
    async with session_scope() as session:
        records = (
            await session.execute(
                select(Record)
                .where(Record.id.in_(record_ids), Record.category.in_(_TRAINEE_CATEGORIES))
                .with_for_update()
            )
        ).scalars().all()
        for record in records:
            await _promote_record(session, record, str(PersonCategory.UNKNOWN))
            record.verification_outcome = RecordVerificationOutcome.VERIFIED_NON_TRAINEE
            changed += 1
        await session.commit()
    return changed


_CURRENT_ROLE = re.compile(
    r"\b(current\s+(?:residents?|fellows?|house\s*staff)|our\s+residents?|residents?|house\s*staff|"
    r"pgy\s*[- ]?[1-9]|resident\s+physician|chief\s+resident|current\s+fellows?)\b",
    re.IGNORECASE,
)
_SUSPICIOUS_CONTEXT = re.compile(
    r"\b(nominat(?:ed|ion)|award|alumni|former\s+resident|graduat(?:ed|e)|completed\s+residency|"
    r"past\s+resident|faculty|medical\s+student|matched|incoming\s+resident|news|article|historical|"
    r"committee|advisory|board|council|membership)\b",
    re.IGNORECASE,
)


def _local_evidence(text: str, name: str) -> str:
    """Return the small, name-local source packet used for deterministic checks.

    The stored record is the first crawl result; this is its one source re-read.
    Restricting the packet makes both the check and the rare adjudication cheap.
    """
    lines = text.splitlines()
    needle = name.casefold().strip()
    for index, line in enumerate(lines):
        if needle and needle in line.casefold():
            start = max(0, index - 4)
            # Preserve a nearby markdown heading, which usually carries roster status.
            for prior in range(index - 1, max(-1, index - 25), -1):
                if lines[prior].lstrip().startswith("#"):
                    start = prior
                    break
            return "\n".join(lines[start:min(len(lines), index + 5)])[:1_500]
    return ""


def _deterministic_current_role(person: RoleCheckInput, text: str) -> RoleDecision | None:
    """Confirm only an unambiguous current trainee claim, never a name hit."""
    evidence = _local_evidence(text, person.full_name)
    if not evidence:
        return None
    # A governance, faculty, historical, recruitment, or non-GME fellow
    # packet is affirmative evidence that this is not a current GME roster
    # entry when it lacks a direct current trainee title/level.  Returning an
    # explicit unknown lets verification correct a legacy false positive to
    # `verified_non_trainee`, instead of misleadingly reporting only
    # `insufficient_evidence`.
    if is_governance_or_non_gme_context(evidence) and not has_direct_current_trainee_evidence(evidence):
        return RoleDecision("unknown", evidence)
    # Negative/historical language wins even if a page also happens to contain
    # "resident" in an article title or biography.
    if _SUSPICIOUS_CONTEXT.search(evidence):
        return None
    expected = (person.category or "unknown").casefold()
    if expected not in ("resident", "fellow"):
        return None
    # PGY labels are meaningful stored crawl evidence.  A re-read with a
    # different PGY is a disagreement, not a cheap confirmation.
    stored_pgy = re.search(r"\bpgy\s*[- ]?([1-9])\b", person.position or "", re.IGNORECASE)
    reread_pgy = re.search(r"\bpgy\s*[- ]?([1-9])\b", evidence, re.IGNORECASE)
    if stored_pgy and (not reread_pgy or stored_pgy.group(1) != reread_pgy.group(1)):
        return None
    if expected == "fellow":
        if re.search(r"\b(current\s+)?fellows?\b", evidence, re.IGNORECASE):
            return RoleDecision("fellow", evidence)
        return None
    if _CURRENT_ROLE.search(evidence):
        return RoleDecision("resident", evidence)
    return None


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

    `page_attempts` is retained for API compatibility; verification deliberately
    does not retry model adjudication or re-read a source repeatedly.
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
        # A role claim cannot be attached safely when the school has multiple
        # active records with the same normalized name. Reconciliation already
        # deduplicates extraction identities; this is a final verification gate
        # for legacy or ambiguous records and intentionally never deletes data.
        name_counts = Counter(str(full_name).casefold().strip() for _, full_name, *_ in rows)
        duplicate_ids = {
            record_id for record_id, full_name, *_ in rows
            if name_counts[str(full_name).casefold().strip()] > 1
        }
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
        semaphore = asyncio.Semaphore(concurrency)
        browser = _Browser()

        async def _ask(url: str, title: str, text: str, people: list[RoleCheckInput]) -> dict | None:
            # Verification gets exactly one adjudication, not a retry loop.
            # The normal path never reaches here at all.
            return await verify_page_roles(url=url, title=title, text=text, people=people, meter=meter)

        async def _one(url: str, title: str | None, people: list[RoleCheckInput]) -> None:
            nonlocal checked, corrected
            # Held for the whole page - fetch, model call and any render - so
            # `concurrency` bounds concurrent model calls too, not just I/O.
            async with semaphore:
                roles_by_record: dict | None = {}
                text, page_title, error = None, title or "", None
                if url not in needs_browser:
                    text, error = await _read_plain(fetcher, url)
                # Plain HTTP was refused, or the crawl only ever read this page
                # in a browser.  A source is re-read only once per verification
                # job; the browser is a fallback for an unreadable HTTP result.
                if text is None:
                    rendered, rendered_title, render_error = await browser.read(url)
                    if rendered is not None:
                        text = rendered
                        page_title = title or rendered_title
                    elif text is None:
                        error = f"{error}; {render_error}" if error else render_error
                if text is None:
                    await _write_outcome(
                        [p.record_id for p in people], RecordVerificationOutcome.SOURCE_UNAVAILABLE,
                    )
                    log.info("verification %s: could not read %s: %s", job_id, url, error)
                    await _audit_attempt(job_id, people, stage="fetch", outcome="unreadable", url=url, detail=error)
                    return
                # Matching a stored crawl label and a re-read is enough only
                # with local, current-role evidence.  A nominee/article/alumni
                # name match is deliberately ambiguous, never auto-confirmed.
                deterministic = {
                    p.record_id: decision
                    for p in people
                    if (decision := _deterministic_current_role(p, text)) is not None
                }
                ambiguous = [p for p in people if p.record_id not in deterministic]
                adjudicated: dict | None = {}
                if ambiguous:
                    adjudicated = await _ask(url, page_title, text, ambiguous)
                model_failed = adjudicated is None
                if model_failed:
                    # The call itself never produced a usable response - a
                    # hard provider/model failure. Distinct from "answered but
                    # grounded nobody" below: that is insufficient evidence,
                    # this is a technical failure to even get an answer.
                    await _write_outcome(
                        [p.record_id for p in ambiguous], RecordVerificationOutcome.VERIFICATION_ERROR,
                    )
                    await _audit_attempt(job_id, ambiguous, stage="model", outcome="error", url=url, detail=meter.last_failure)
                    roles_by_record = deterministic
                else:
                    roles_by_record = {**deterministic, **adjudicated}
                if not roles_by_record:
                    if model_failed:
                        # The technical error above is the outcome; do not
                        # overwrite it with an evidence judgement.
                        return
                    # Read, and the model answered, but grounded nobody on
                    # this page - not the same as "nothing needed correcting".
                    await _write_outcome([p.record_id for p in people], RecordVerificationOutcome.INSUFFICIENT_EVIDENCE)
                    downgraded = await _downgrade_ungrounded_trainees([p.record_id for p in people])
                    if downgraded:
                        async with session_scope() as job_session:
                            await job_session.execute(
                                update(VerificationJob)
                                .where(VerificationJob.id == job_id)
                                .values(
                                    records_checked=VerificationJob.records_checked + downgraded,
                                    records_corrected=VerificationJob.records_corrected + downgraded,
                                    updated_at=func.now(),
                                )
                            )
                        checked += downgraded
                        corrected += downgraded
                    await _audit_attempt(job_id, people, stage="model", outcome="no_decision", url=url, detail=meter.last_failure)
                    return
                # The page was readable, but a person without a source-backed
                # decision remains unverified rather than silently retaining
                # a possibly wrong crawl label.
                ungrounded_ids = [
                    p.record_id for p in people
                    if p.record_id not in roles_by_record and not (model_failed and p in ambiguous)
                ]
            downgraded_ungrounded = 0
            if ungrounded_ids:
                await _write_outcome(ungrounded_ids, RecordVerificationOutcome.INSUFFICIENT_EVIDENCE)
                downgraded_ungrounded = await _downgrade_ungrounded_trainees(ungrounded_ids)
            prior = {p.record_id: p.category for p in people}
            now = datetime.now(UTC)
            page_checked = page_corrected = downgraded_ungrounded
            async with session_scope() as write_session:
                for record_id, decision in roles_by_record.items():
                    record = await write_session.get(Record, record_id, with_for_update=True)
                    if record is None:
                        continue
                    confidence, risk, reason = _verification_quality(
                        role=decision.role, evidence=decision.evidence, position=record.position,
                        url=url, title=title or page_title, duplicate_name=record_id in duplicate_ids,
                    )
                    record.verification_confidence = confidence
                    record.verification_risk = risk
                    record.verification_reason = reason
                    record.verification_evidence = decision.evidence
                    # High-risk trainee claims are deliberately not promoted:
                    # grounded, but not trustworthy enough to confirm. Treated
                    # as insufficient evidence for accounting purposes, with
                    # the risk detail above kept for whoever reviews it.
                    if risk != "verified" and decision.role in ("resident", "fellow"):
                        record.verification_outcome = RecordVerificationOutcome.INSUFFICIENT_EVIDENCE
                        page_checked += 1
                        continue
                    page_checked += 1
                    # `roles` remains an API-compatible stored field, but a
                    # verified record now has exactly one canonical role.
                    record.roles = [decision.role]
                    record.roles_checked_at = now
                    record.verification_outcome = {
                        "resident": RecordVerificationOutcome.VERIFIED_RESIDENT,
                        "fellow": RecordVerificationOutcome.VERIFIED_FELLOW,
                    }.get(decision.role, RecordVerificationOutcome.VERIFIED_NON_TRAINEE)
                    if decision.role != record.category:
                        await _promote_record(write_session, record, decision.role)
                    if decision.role != prior.get(record_id):
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
            await _audit_attempt(job_id, people, stage="decision", outcome="verified" if all(p.record_id in roles_by_record for p in people) else "partial", url=url)

        timed_out = False
        try:
            # This starts here, inside the worker, not at crawl creation or
            # queueing.  Cancelling unfinished page tasks leaves their records
            # untouched (null outcome), so a resume can pick them up.
            async with asyncio.timeout(_VERIFICATION_LIMIT.total_seconds()):
                async with Fetcher() as fetcher:
                    await asyncio.gather(*(
                        _one(url, title, people) for (url, title), people in by_page.items()
                    ))
        except TimeoutError:
            timed_out = True
            log.warning("verification %s reached its active four-hour limit", job_id)
        finally:
            await browser.stop()

        # Complete accounting: every targeted record's *current*
        # verification_outcome, tallied fresh rather than from in-process
        # counters, so it reflects exactly what is on the records regardless
        # of how far this pass got. Null (never attempted) is reported as
        # NOT_ATTEMPTED without being a stored enum value.
        target_ids = [row[0] for row in rows]
        async with session_scope() as session:
            outcome_counts: dict[str | None, int] = (
                dict(
                    (
                        await session.execute(
                            select(Record.verification_outcome, func.count())
                            .where(Record.id.in_(target_ids))
                            .group_by(Record.verification_outcome)
                        )
                    ).all()
                )
                if target_ids
                else {}
            )
        not_attempted = outcome_counts.pop(None, 0)
        attempted = sum(outcome_counts.values())
        confirmed = sum(outcome_counts.get(o, 0) for o in _CONFIRMED_OUTCOMES)
        unresolved_total = attempted - confirmed
        parts = [f"{count} {outcome}" for outcome, count in sorted(outcome_counts.items())]
        if not_attempted:
            parts.append(f"{not_attempted} NOT_ATTEMPTED")
        summary = ", ".join(parts)

        # A pass that reached nobody is the checker never running, not a
        # clean bill of health: report it as a failure with the reason.
        total_failure = attempted == 0 and bool(rows)
        async with session_scope() as session:
            await session.execute(
                update(VerificationJob)
                .where(VerificationJob.id == job_id)
                .values(
                    status=(VerificationStatus.FAILED if (timed_out or total_failure)
                            else VerificationStatus.COMPLETED),
                    records_checked=checked,
                    records_corrected=corrected,
                    error=(
                        ("active verification limit reached; completed decisions were saved and "
                         "remaining records are pending and may be resumed")[:500]
                        if timed_out else f"reached 0 of {len(rows)} targeted records: {summary or 'nothing attempted'}"[:500]
                        if total_failure
                        else (
                            f"{summary} (of {len(rows)} targeted)"[:500]
                            if (unresolved_total or not_attempted)
                            else None
                        )
                    ),
                    finished_at=datetime.now(UTC),
                )
            )
        log.info(
            "verification %s: %d/%d records checked, %d corrected, %d pages rendered; outcomes: %s",
            job_id, checked, len(rows), corrected, browser.rendered, summary or "none",
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
