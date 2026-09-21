"""Stage 6: mark departures, persist the fingerprint, close out the SiteRun."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, update

from ...db.enums import SiteRunStatus
from ...db.models import Record, SiteRun
from ...db.repositories.programs import refresh_program_counts
from ...db.repositories.records import lock_site, mark_missing_records
from ...db.repositories.sites import save_fingerprint, set_dominant_specialty, visited_hashes
from ...llm.planner import FOUND
from ...orchestrator.events import EventType
from ...storage.artifacts import replace_school_screenshots
from ..checkpoint import clear_checkpoint
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.finalize")


async def finalize(state: SiteState, deps: PipelineDeps) -> SiteState:
    site_id = state["site_id"]
    site_run_id = state["site_run_id"]
    missing = 0
    # A directory-only run visits no pages and captures no screenshots: there
    # is nothing to mark missing and no screenshot set to replace.
    crawled = "crawl" in (state.get("modes") or ["crawl"]) and state.get("status") != "skipped"

    if state.get("status") != "rejected":
        async with deps.sessionmaker() as session:
            await lock_site(session, site_id)
            visited = await visited_hashes(session, site_run_id)
            if crawled:
                missing = await mark_missing_records(
                    session,
                    site_id=site_id,
                    seen_record_ids=set(state.get("seen_record_ids", [])),
                    visited_url_hashes=visited,
                )
                if state.get("fingerprint"):
                    await save_fingerprint(session, site_id, state["fingerprint"])
                await _update_dominant_specialty(session, site_id)
                await refresh_program_counts(session, site_id)
            await session.commit()

        # A school keeps only its most recent run's screenshots. Skipped runs
        # capture nothing, so replacing there would delete the previous set and
        # leave the school with no screenshots at all.
        if crawled:
            await replace_school_screenshots(site_id, site_run_id)

    status = _final_status(state)
    found = (
        state.get("records_new", 0) + state.get("records_changed", 0)
        + state.get("records_unchanged", 0)
    )
    reason = _no_people_reason(state, status, found)
    async with deps.sessionmaker() as session:
        # The site is finished; a stale checkpoint must not resurrect it.
        await clear_checkpoint(session, site_run_id)
        await session.execute(
            update(SiteRun)
            .where(SiteRun.id == site_run_id)
            .values(
                status=status,
                skip_reason=state.get("skip_reason"),
                similarity_score=state.get("similarity_score"),
                steps_taken=state.get("steps_taken", 0),
                records_found=(
                    state.get("records_new", 0)
                    + state.get("records_changed", 0)
                    + state.get("records_unchanged", 0)
                ),
                records_new=state.get("records_new", 0),
                records_changed=state.get("records_changed", 0),
                records_missing=missing,
                known_path_hits=state.get("known_path_hits", 0),
                candidates_considered=state.get("candidates_considered", 0),
                # A school that finished with nobody says why, instead of reading
                # as a clean "completed". Recorded here only: the pipeline's own
                # `error_code` decides routing and must stay unset.
                error_code=state.get("error_code") or (reason[0] if reason else None),
                error_message=state.get("error_message") or (reason[1] if reason else None),
                coverage=_coverage(state),
                tokens_in=deps.meter.total.input_tokens,
                tokens_out=deps.meter.total.output_tokens,
                spend_usd=deps.meter.cost_usd,
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()

    event = {
        SiteRunStatus.COMPLETED: EventType.SITE_COMPLETED,
        SiteRunStatus.SKIPPED: EventType.SITE_SKIPPED,
        SiteRunStatus.REJECTED: EventType.SITE_REJECTED,
        SiteRunStatus.FAILED: EventType.SITE_FAILED,
    }.get(status, EventType.SITE_COMPLETED)

    await deps.emitter.emit(
        event,
        site_id=site_id,
        site_run_id=site_run_id,
        domain=state["root_domain"],
        status=str(status),
        records_new=state.get("records_new", 0),
        records_changed=state.get("records_changed", 0),
        records_unchanged=state.get("records_unchanged", 0),
        records_missing=missing,
        steps_taken=state.get("steps_taken", 0),
        reason=state.get("error_message") or state.get("skip_reason"),
    )

    log.info(
        "site %s finished: %s (new=%d changed=%d unchanged=%d missing=%d steps=%d)",
        state["root_domain"], status, state.get("records_new", 0),
        state.get("records_changed", 0), state.get("records_unchanged", 0),
        missing, state.get("steps_taken", 0),
    )

    return {**state, "status": str(status), "records_missing": missing, "terminated": True}


def _coverage(state: SiteState) -> dict | None:
    """The program plan as it ended: how many programs were found and how many
    have a roster. Bounded, since some schools list hundreds of programs."""
    programs = state.get("programs") or []
    if not programs:
        return None
    covered = sum(1 for p in programs if p.get("status") == FOUND)
    return {
        "programs_total": len(programs),
        "programs_covered": covered,
        "pages_read": state.get("steps_taken", 0),
        "programs": [
            {"name": p.get("name"), "kind": p.get("kind"), "status": p.get("status"),
             "people": p.get("people", 0)}
            for p in programs[:300]
        ],
    }


def _no_people_reason(state: SiteState, status: SiteRunStatus, found: int) -> tuple[str, str] | None:
    """Why a school that was crawled produced no people, in words for a person."""
    if status != SiteRunStatus.COMPLETED or found > 0:
        return None
    if "crawl" not in (state.get("modes") or ["crawl"]):
        return None
    steps = state.get("steps_taken", 0)
    programs = state.get("programs") or []
    if not state.get("candidates"):
        return (
            "NO_PAGES_TO_READ",
            "No page that could list residents or fellows was found on this site. "
            "Its entry page may need a login or scripts the crawler could not run, "
            "or the school may not publish its trainees.",
        )
    listed = f" ({len(programs)} programs found, none with a roster)" if programs else ""
    return (
        "NO_PEOPLE_FOUND",
        f"Read {steps:,} pages{listed} and found no residents or fellows. The school may not "
        "publish current trainees, or may publish them as pictures or PDF files the crawler cannot read.",
    )


def _final_status(state: SiteState) -> SiteRunStatus:
    status = state.get("status")
    if status == "rejected":
        return SiteRunStatus.REJECTED
    if status == "skipped":
        return SiteRunStatus.SKIPPED
    if status == "failed":
        return SiteRunStatus.FAILED
    if status == "cancelled":
        return SiteRunStatus.CANCELLED
    return SiteRunStatus.COMPLETED


async def _update_dominant_specialty(session, site_id: str) -> None:
    """The site's most common specialty, used as a fallback for ambiguous pages."""
    rows = await session.execute(
        select(Record.specialty_normalized).where(
            Record.site_id == site_id, Record.specialty_normalized.isnot(None)
        )
    )
    counts = Counter(v for v in rows.scalars().all() if v)
    if counts:
        await set_dominant_specialty(session, site_id, counts.most_common(1)[0][0])
