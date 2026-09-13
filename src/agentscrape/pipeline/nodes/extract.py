"""Stage 4 and 5: work the candidate list, extracting and reconciling as we go.

Cost discipline, cheapest first:
  1. plain HTML fetch (no browser)  -> sufficient for most static rosters
  2. rendered page + screenshot     -> only when HTML came up empty on a page
                                       that looks like a roster
  3. one interaction step           -> only when the accessibility tree offers
                                       pagination or a "load more" control

Reconciliation runs per batch rather than once at the end, so a run that is
stopped mid-site still has valid, persisted partial results.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from ...config import settings
from ...db.enums import ExtractionMethod, FetchMode
from ...db.repositories.records import ExtractionContext, lock_site, reconcile_people
from ...db.repositories.sites import claim_url, record_path_outcome, update_visit
from ...extraction.html_people import extract_people, page_looks_thin
from ...extraction.person import ExtractedPerson
from ...extraction.vision import extract_with_vision
from ...orchestrator.events import EventType
from ...storage.artifacts import relative_path, save_screenshot, screenshot_expiry
from ...urls import canonicalize, host_of, url_hash
from ..checkpoint import save_checkpoint
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.extract")

# How many candidates to pull per loop iteration. Cheap fetches run concurrently
# inside a batch; any browser work in the batch is serialized afterwards.
BATCH_SIZE = 8


async def extract_batch(state: SiteState, deps: PipelineDeps) -> SiteState:
    """Process one batch of candidates and fold the results into the state."""
    candidates = state.get("candidates", [])
    cursor = state.get("cursor", 0)
    steps_taken = state.get("steps_taken", 0)
    budget = state["step_budget"]

    remaining_budget = max(budget - steps_taken, 0)
    batch = candidates[cursor : cursor + min(BATCH_SIZE, remaining_budget)]
    if not batch:
        return {**state, "cursor": len(candidates)}

    # Claim URLs so two entry points inside one site never fetch the same page,
    # and so a resumed run skips what it already did.
    claimed: list[dict] = []
    async with deps.sessionmaker() as session:
        for candidate in batch:
            if await claim_url(session, state["site_run_id"], candidate["url"]):
                claimed.append(candidate)
        await session.commit()

    if not claimed:
        return {**state, "cursor": cursor + len(batch)}

    results = await deps.fetcher.get_many([c["url"] for c in claimed])

    new = changed = unchanged = missing_total = 0
    known_hits = state.get("known_path_hits", 0)
    seen_ids = list(state.get("seen_record_ids", []))
    fingerprint = dict(state.get("fingerprint", {}))
    steps_used = 0

    for candidate, result in zip(claimed, results, strict=True):
        if deps.stop_requested():
            log.info("stop requested; halting extraction for %s", state["root_domain"])
            break

        steps_used += 1
        url = result.final_url or candidate["url"]
        people: list[ExtractedPerson] = []
        fetch_mode = FetchMode.HTML
        screenshot_rel: str | None = None
        field_locations: dict[str, dict[str, int]] = {}
        page_title = ""

        if result.ok and result.is_html:
            page_title = _title_of(result.text)
            people = extract_people(result.text, page_title=page_title, url=url)

        should_render, why = (
            page_looks_thin(result.text, result.text, len(people))
            if result.ok
            else (True, f"fetch failed: {result.error}")
        )

        if should_render and deps.can_render and steps_taken + steps_used < budget:
            steps_used += 1
            log.info("escalating to browser for %s (%s)", url, why)
            rendered = await _render_and_extract(
                deps, url, state, candidate, why
            )
            if rendered is not None:
                people, fetch_mode, screenshot_rel, field_locations, page_title = rendered

        records_here = 0
        if people:
            context = ExtractionContext(
                site_id=state["site_id"],
                site_host=state["root_domain"],
                source_url=url,
                page_title=page_title or None,
                extraction_method=(
                    ExtractionMethod.KNOWN_PATH
                    if candidate.get("is_known_path")
                    else ExtractionMethod.DISCOVERY
                ),
                fetch_mode=fetch_mode,
                screenshot_path=screenshot_rel,
                screenshot_expires_at=screenshot_expiry() if screenshot_rel else None,
                field_locations=field_locations,
                page_score=float(candidate.get("score", 0.0)),
                run_id=state.get("run_id"),
                site_run_id=state["site_run_id"],
                captured_at=datetime.now(UTC),
                site_dominant_specialty=state.get("dominant_specialty"),
            )
            async with deps.sessionmaker() as session:
                # Sites run in parallel; one site's reconciliation never does.
                await lock_site(session, state["site_id"])
                outcome = await reconcile_people(session, people, context)
                await session.commit()

            new += outcome.new
            changed += outcome.changed
            unchanged += outcome.unchanged
            seen_ids.extend(outcome.record_ids)
            records_here = outcome.total_seen

            # Fingerprint only the pages that actually produced records: those
            # are the ones the next run's skip probe will re-fetch, and the two
            # sets have to line up for the cheap comparison to ever match. The
            # hash is of the plain HTTP body even when the records came from a
            # render, because the probe is plain HTTP too.
            if result.ok and result.is_html:
                fingerprint[canonicalize(url) or url] = result.content_hash

            if candidate.get("is_known_path") and records_here:
                known_hits += 1
                await deps.emitter.emit(
                    EventType.KNOWN_PATH_HIT,
                    site_id=state["site_id"], site_run_id=state["site_run_id"],
                    url=url, records=records_here,
                )

        async with deps.sessionmaker() as session:
            await update_visit(
                session,
                site_run_id=state["site_run_id"],
                url=candidate["url"],
                fetch_mode=str(fetch_mode),
                http_status=result.status,
                content_hash=result.content_hash if result.ok else None,
                records_yielded=records_here,
                error=result.error,
            )
            await record_path_outcome(
                session,
                site_id=state["site_id"],
                url=url,
                records_found=records_here,
                content_hash=result.content_hash if result.ok else None,
            )
            await session.commit()

        await deps.emitter.emit(
            EventType.SITE_STEP,
            site_id=state["site_id"],
            site_run_id=state["site_run_id"],
            url=url,
            action=f"fetch:{fetch_mode}",
            records=records_here,
            screenshot_url=(
                f"/api/v1/artifacts/{screenshot_rel}" if screenshot_rel else None
            ),
            steps_taken=steps_taken + steps_used,
        )

    updated: SiteState = {
        **state,
        "cursor": cursor + len(batch),
        "steps_taken": steps_taken + steps_used,
        "records_new": state.get("records_new", 0) + new,
        "records_changed": state.get("records_changed", 0) + changed,
        "records_unchanged": state.get("records_unchanged", 0) + unchanged,
        "records_missing": state.get("records_missing", 0) + missing_total,
        "seen_record_ids": seen_ids,
        "known_path_hits": known_hits,
        "fingerprint": fingerprint,
    }

    # Checkpoint after every batch, so an interrupted site resumes here instead
    # of re-running discovery, which is the expensive stage.
    async with deps.sessionmaker() as session:
        await save_checkpoint(session, updated)
        await session.commit()

    return updated


async def _render_and_extract(
    deps: PipelineDeps, url: str, state: SiteState, candidate: dict, why: str
):
    """Browser escalation: render, screenshot, read with vision, measure boxes."""
    from ...browser.renderer import click_by_accessible_name, render_page

    try:
        rendered = await asyncio.wait_for(
            render_page(deps.browser_context, url, capture_screenshot=True),
            timeout=settings.page_timeout_seconds + 15,
        )
    except TimeoutError:
        log.warning("render timed out for %s", url)
        return None
    if not rendered.ok:
        log.warning("render failed for %s: %s", url, rendered.error)
        return None

    people = extract_people(rendered.html, page_title=rendered.title, url=url)

    # One interaction step, only when the accessibility tree offers a control.
    # Targets come from role + name; never from pixel coordinates.
    if not people and rendered.controls:
        control = next((c for c in rendered.controls if not c.get("disabled")), None)
        if control:
            log.info("interacting with %r on %s", control["name"], url)
            after = await click_by_accessible_name(
                deps.browser_context, url, control["role"], control["name"]
            )
            if after.ok:
                people = extract_people(after.html, page_title=after.title, url=url)

    if not people:
        # Vision reads what the DOM does not expose: contacts published as images,
        # or layout that carries meaning.
        people = await extract_with_vision(
            url=url, title=rendered.title, text=rendered.text,
            screenshot=rendered.screenshot, meter=deps.meter,
        )

    # Measure where each value sits, from the real DOM rather than the model.
    field_locations = rendered.field_locations
    hints = [h for person in people for h in person.locate_hints]
    if hints and not field_locations:
        located = await render_page(
            deps.browser_context, url, capture_screenshot=False, locate=hints
        )
        field_locations = located.field_locations if located.ok else {}

    screenshot_rel = None
    if rendered.screenshot:
        path = save_screenshot(
            rendered.screenshot,
            site_run_id=state["site_run_id"],
            url_hash=url_hash(url),
        )
        screenshot_rel = relative_path(path)

    return people, FetchMode.BOTH, screenshot_rel, field_locations, rendered.title


def _title_of(html: str) -> str:
    from selectolax.parser import HTMLParser

    node = HTMLParser(html).css_first("title")
    return node.text().strip() if node else ""


def host_for(url: str) -> str:
    return host_of(url)
