"""Directory search: look people up in the school's directory to fill blanks.

    ... crawl ... -> directory -> finalize        (crawl + directory)
    entry -> directory -> finalize                 (directory only)

Runs after the crawl, or alone on a school that already has people. Who is
looked up: named people not marked missing who lack an address or both a PGY
and a class year — residents and fellows first. On a combined run only the
people this crawl saw are looked up, so someone who has left is not refreshed
by the directory just before the crawl would have marked them missing.

Only blank fields are filled (see `fill_record_blanks`); what a roster page
printed always wins. Progress is checkpointed per batch, so a stopped run
resumes where it left off, and how to search the directory is stored on the
site so later runs skip learning it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import case, or_, select

from ...config import settings
from ...db.enums import ExtractionMethod, PersonCategory, RecordStatus
from ...db.models import Record, Site
from ...db.repositories.records import ExtractionContext, fill_record_blanks, lock_site
from ...directory.learn import BROWSER, UNAVAILABLE, learn_directory
from ...directory.lookup import lookup_person
from ..checkpoint import save_checkpoint
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.directory")

LOOKUP_BATCH = 8
NOTE_EVERY = 100
_ORDER = case(
    (Record.category.in_([PersonCategory.RESIDENT, PersonCategory.FELLOW]), 0),
    (Record.category == PersonCategory.STUDENT, 1),
    (Record.category == PersonCategory.UNKNOWN, 2),
    else_=3,
)


async def _targets(deps: PipelineDeps, state: SiteState, limit: int) -> list[Record]:
    done = set(state.get("directory_done", []))
    statement = (
        select(Record)
        .where(
            Record.site_id == state["site_id"],
            Record.status != RecordStatus.MISSING,
            Record.role_account.is_(False),
            Record.full_name.isnot(None),
            or_(
                Record.email.is_(None),
                Record.pgy_at_capture.is_(None) & Record.class_of.is_(None),
            ),
        )
        .order_by(_ORDER, Record.full_name)
    )
    if "crawl" in (state.get("modes") or []):
        statement = statement.where(Record.id.in_(state.get("seen_record_ids") or [""]))
    async with deps.sessionmaker() as session:
        rows = (await session.execute(statement)).scalars().all()
    return [r for r in rows if r.id not in done and " " in (r.full_name or "").strip()][:limit]


async def directory_search(state: SiteState, deps: PipelineDeps) -> SiteState:
    state = {**state, "crawl_done": True}
    stats = dict(state.get("directory_stats") or {})
    for key in ("looked_up", "matched", "filled", "emails", "years", "not_listed", "ambiguous"):
        stats.setdefault(key, 0)

    async with deps.sessionmaker() as session:
        site = await session.get(Site, state["site_id"])
        directory_url = site.directory_url if site else None
        config = dict(site.directory_config or {}) if site else {}
    if not directory_url:
        await deps.note(state, "No directory link for this school; skipping directory search")
        return {**state, "directory_stats": stats}

    targets = await _targets(deps, state, settings.directory_max_lookups)
    if not targets:
        await deps.note(state, "Directory search: nobody is missing an address or year")
        return {**state, "directory_stats": stats}

    if config.get("directory_url") != directory_url or not config.get("mode"):
        await deps.note(state, f"Learning how to search the directory at {directory_url}")
        config = await learn_directory(deps, directory_url, [t.full_name for t in targets[:5]])
        async with deps.sessionmaker() as session:
            site = await session.get(Site, state["site_id"])
            if site is not None:
                site.directory_config = config
            await session.commit()
    if config.get("mode") == UNAVAILABLE:
        await deps.note(state, f"Directory search unavailable: {config.get('reason')}")
        return {**state, "directory_stats": {**stats, "unavailable": 1}}

    if config.get("mode") == BROWSER:
        if not deps.can_render:
            await deps.note(state, "Directory search needs a browser, which this run does not have")
            return {**state, "directory_stats": stats}
        targets = targets[: settings.directory_max_browser_lookups]

    await deps.note(
        state, f"Searching the directory for {len(targets):,} people missing an address or year"
    )
    done = list(state.get("directory_done", []))
    changed = 0
    for start in range(0, len(targets), LOOKUP_BATCH):
        if deps.stop_requested():
            log.info("stop requested; halting directory search for %s", state["root_domain"])
            break
        batch = targets[start : start + LOOKUP_BATCH]
        results = await asyncio.gather(*(lookup_person(deps, config, r.full_name) for r in batch))
        async with deps.sessionmaker() as session:
            await lock_site(session, state["site_id"])
            for record, result in zip(batch, results, strict=True):
                stats["looked_up"] += 1
                done.append(record.id)
                if result.person is None:
                    if result.reason == "not listed":
                        stats["not_listed"] += 1
                    elif result.reason == "ambiguous":
                        stats["ambiguous"] += 1
                    continue
                stats["matched"] += 1
                current = await session.get(Record, record.id)
                if current is None:
                    continue
                filled = await fill_record_blanks(
                    session, current, result.person,
                    ExtractionContext(
                        site_id=state["site_id"], site_host=state["root_domain"],
                        source_url=result.url, page_title=result.title or None,
                        extraction_method=ExtractionMethod.DIRECTORY,
                        fetch_mode=result.fetch_mode, run_id=state.get("run_id"),
                        site_run_id=state["site_run_id"], captured_at=datetime.now(UTC),
                        site_dominant_specialty=state.get("dominant_specialty"),
                    ),
                )
                if filled:
                    changed += 1
                    stats["filled"] += 1
                    stats["emails"] += "email" in filled
                    stats["years"] += bool({"pgy", "class_of"} & set(filled))
            await session.commit()

        state = {**state, "directory_done": done, "directory_stats": dict(stats),
                 "records_changed": state.get("records_changed", 0) + changed}
        changed = 0
        async with deps.sessionmaker() as session:
            await save_checkpoint(session, state)
            await session.commit()
        if stats["looked_up"] % NOTE_EVERY < LOOKUP_BATCH:
            await deps.note(state, _summary(stats))

    await deps.note(state, _summary(stats), directory=stats)
    log.info("directory search for %s: %s", state["root_domain"], stats)
    return state


def _summary(stats: dict[str, int]) -> str:
    return (
        f"Directory: {stats['looked_up']:,} looked up, {stats['matched']:,} matched, "
        f"{stats['emails']:,} emails and {stats['years']:,} years filled"
    )
