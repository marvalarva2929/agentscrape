"""Stage 4 and 5: work the frontier, with the model reading every page.

For each page:
  1. plain HTML fetch
  2. the model reads the page text (hidden tabs, mailto addresses, alt text and
     embedded JSON included) and says who is on it, what kind of page it is,
     and whether people are hidden or missing
  3. escalate to a rendered page when the model says the people are not in the
     HTML, or it counts more people than it could read; activate up to five
     tabs / "load more" controls it picks from the accessibility tree; read the
     screenshot with the vision model when text still falls short
  4. every link on the page (with its anchor text and heading) goes to the
     model for triage, and the unvisited tail is re-sorted by its priority

The regex extractor still runs on every page, to fill in addresses and as the
fallback whenever a model call fails.

Reconciliation runs per batch rather than once at the end, so a run that is
stopped mid-site still has valid, persisted partial results.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ...config import settings
from ...db.enums import ExtractionMethod, FetchMode, PersonCategory
from ...db.repositories.records import ExtractionContext, lock_site, reconcile_people
from ...db.repositories.sites import claim_url, record_path_outcome, update_visit
from ...discovery.sitemap import LinkContext, extract_link_contexts
from ...extraction.html_people import extract_people, page_looks_thin
from ...extraction.person import ExtractedPerson
from ...extraction.text import html_to_model_text
from ...llm.navigation import decide_navigation
from ...llm.planner import FOUND, PENDING, match_program
from ...llm.reader import PageReading, combine_with_regex, fold, merge_people, read_page
from ...llm.triage import triage_links
from ...orchestrator.events import EventType
from ...storage.artifacts import (
    png_dimensions,
    relative_path,
    save_screenshot,
    screenshot_expiry,
)
from ...urls import canonicalize, host_of, in_scope, registrable_domain, url_hash
from ..checkpoint import save_checkpoint
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.extract")

# Pages per loop iteration. Fetches and model reads in a batch run concurrently;
# browser work is serialized on the site's one browser context.
BATCH_SIZE = 16
_TRAINEES = (PersonCategory.RESIDENT, PersonCategory.FELLOW)
# How many visited pages to remember per program for gap filling.
_PROGRAM_VISITS_KEPT = 60


@dataclass
class PageOutcome:
    url: str
    people: list[ExtractedPerson] = field(default_factory=list)
    fetch_mode: FetchMode = FetchMode.HTML
    screenshot_rel: str | None = None
    shot_size: tuple[int | None, int | None] = (None, None)
    field_locations: dict[str, dict[str, int]] = field(default_factory=dict)
    title: str = ""
    links: list[LinkContext] = field(default_factory=list)
    reading: PageReading | None = None
    steps: int = 1
    duplicate: bool = False


def link_key(url: str) -> str:
    """Identity for "have we already considered this link". Case-folded, because
    CMSs serve the same page under several capitalizations of its path and
    fetching each one spent Baylor Scott & White's budget three times over."""
    return url_hash(url.lower())[:20]


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
    allowed = set(
        state.get("allowed_domains") or [registrable_domain(state["root_domain"])]
    )
    processed_hashes = set(state.get("processed_hashes", []))

    outcomes = await asyncio.gather(*(
        _process_page(deps, state, candidate, result, processed_hashes)
        for candidate, result in zip(claimed, results, strict=True)
    ))

    new = changed = unchanged = 0
    known_hits = state.get("known_path_hits", 0)
    barren_streak = state.get("barren_streak", 0)
    seen_ids = list(state.get("seen_record_ids", []))
    fingerprint = dict(state.get("fingerprint", {}))
    programs = [dict(p) for p in state.get("programs", [])]
    steps_used = 0
    new_links: dict[str, dict] = {}

    for candidate, result, outcome in zip(claimed, results, outcomes, strict=True):
        if deps.stop_requested():
            log.info("stop requested; halting extraction for %s", state["root_domain"])
            break
        steps_used += outcome.steps
        url = outcome.url
        if result.ok and result.is_html:
            processed_hashes.add(result.content_hash)

        records_here = 0
        trainees_here = sum(1 for p in outcome.people if p.category in _TRAINEES)
        if outcome.people:
            context = ExtractionContext(
                site_id=state["site_id"],
                site_host=state["root_domain"],
                source_url=url,
                page_title=outcome.title or None,
                extraction_method=(
                    ExtractionMethod.KNOWN_PATH
                    if candidate.get("is_known_path")
                    else ExtractionMethod.DISCOVERY
                ),
                fetch_mode=outcome.fetch_mode,
                screenshot_path=outcome.screenshot_rel,
                screenshot_expires_at=screenshot_expiry() if outcome.screenshot_rel else None,
                screenshot_width=outcome.shot_size[0],
                screenshot_height=outcome.shot_size[1],
                field_locations=outcome.field_locations,
                page_score=float(candidate.get("priority", candidate.get("score", 0.0))),
                run_id=state.get("run_id"),
                site_run_id=state["site_run_id"],
                captured_at=datetime.now(UTC),
                site_dominant_specialty=state.get("dominant_specialty"),
            )
            async with deps.sessionmaker() as session:
                # Sites run in parallel; one site's reconciliation never does.
                await lock_site(session, state["site_id"])
                reconciled = await reconcile_people(session, outcome.people, context)
                await session.commit()

            new += reconciled.new
            changed += reconciled.changed
            unchanged += reconciled.unchanged
            seen_ids.extend(reconciled.record_ids)
            records_here = reconciled.total_seen

            # Fingerprint only the pages that produced records: those are the
            # ones the next run's skip probe re-fetches. The hash is of the
            # plain HTTP body even when records came from a render, because the
            # probe is plain HTTP too.
            if result.ok and result.is_html:
                fingerprint[canonicalize(url) or url] = result.content_hash

            if candidate.get("is_known_path") and records_here:
                known_hits += 1
                await deps.emitter.emit(
                    EventType.KNOWN_PATH_HIT,
                    site_id=state["site_id"], site_run_id=state["site_run_id"],
                    url=url, records=records_here,
                )

        _update_programs(programs, candidate, outcome, trainees_here)

        # Last-resort stop signal only; program coverage and link priority are
        # the real ones. A page we could not read is evidence of blocking, not
        # of the site running dry, so it does not count.
        if records_here:
            barren_streak = 0
        elif result.ok and not outcome.duplicate:
            barren_streak += 1

        for link in outcome.links:
            new_links.setdefault(link.url, link.as_dict())

        async with deps.sessionmaker() as session:
            await update_visit(
                session,
                site_run_id=state["site_run_id"],
                url=candidate["url"],
                fetch_mode=str(outcome.fetch_mode),
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
            action=f"fetch:{outcome.fetch_mode}",
            records=records_here,
            screenshot_url=(
                f"/api/v1/artifacts/{outcome.screenshot_rel}" if outcome.screenshot_rel else None
            ),
            steps_taken=steps_taken + steps_used,
        )

    triaged = set(state.get("triaged", []))
    candidates, triaged = await _grow_frontier(
        deps, state, candidates, cursor + len(batch), new_links,
        allowed=allowed, triaged=triaged, programs=programs,
    )

    updated: SiteState = {
        **state,
        "candidates": candidates,
        "cursor": cursor + len(batch),
        "steps_taken": steps_taken + steps_used,
        "records_new": state.get("records_new", 0) + new,
        "records_changed": state.get("records_changed", 0) + changed,
        "records_unchanged": state.get("records_unchanged", 0) + unchanged,
        "seen_record_ids": seen_ids,
        "known_path_hits": known_hits,
        "barren_streak": barren_streak,
        "fingerprint": fingerprint,
        "programs": programs,
        "triaged": sorted(triaged),
        "processed_hashes": sorted(processed_hashes),
    }

    # Checkpoint after every batch, so an interrupted site resumes here instead
    # of re-running discovery, which is the expensive stage.
    async with deps.sessionmaker() as session:
        await save_checkpoint(session, updated)
        await session.commit()

    return updated


async def _process_page(
    deps: PipelineDeps, state: SiteState, candidate: dict, result, processed_hashes: set[str]
) -> PageOutcome:
    url = result.final_url or candidate["url"]
    outcome = PageOutcome(url=url)
    allowed = set(state.get("allowed_domains") or [registrable_domain(state["root_domain"])])

    if result.ok and result.is_html:
        if result.content_hash in processed_hashes:
            # Same body under another URL (case variants, mirrors, redirects).
            # Everything on it was already read; it costs no step.
            outcome.duplicate = True
            outcome.steps = 0
            return outcome
        outcome.title = _title_of(result.text)
        outcome.links = extract_link_contexts(result.text, url, allowed_domains=allowed)
        text = html_to_model_text(result.text)
        regex_people = extract_people(result.text, page_title=outcome.title, url=url)
        reading = await read_page(url=url, title=outcome.title, text=text, meter=deps.meter)
        outcome.reading = reading
        if reading.ok:
            outcome.people = combine_with_regex(reading.people, regex_people, fold(text))
            should_render = reading.needs_render or reading.looks_incomplete
            why = reading.render_reason or reading.hidden_content or (
                f"model counted {reading.expected_people_count}, read {len(reading.people)}"
            )
        else:
            outcome.people = regex_people
            should_render, why = page_looks_thin(result.text, result.text, len(regex_people))
    else:
        should_render, why = True, f"fetch failed: {result.error or result.status}"

    budget_left = state["step_budget"] - state.get("steps_taken", 0)
    if should_render and deps.can_render and budget_left > 0:
        outcome.steps += 1
        log.info("escalating to browser for %s (%s)", url, why)
        async with _render_lock(deps):
            rendered = await _render_and_extract(deps, url, state, outcome)
        if rendered is not None:
            outcome.links = _merge_links(outcome.links, rendered.links)
    return outcome


def _render_lock(deps: PipelineDeps) -> asyncio.Lock:
    """One browser context per site, so browser work within a batch is serial."""
    lock = getattr(deps, "_render_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        deps._render_lock = lock  # type: ignore[attr-defined]
    return lock


def _merge_links(first: list[LinkContext], second: list[LinkContext]) -> list[LinkContext]:
    seen = {link.url for link in first}
    return [*first, *(link for link in second if link.url not in seen)]


@dataclass
class _Rendered:
    links: list[LinkContext]


async def _render_and_extract(
    deps: PipelineDeps, url: str, state: SiteState, outcome: PageOutcome
) -> _Rendered | None:
    """Browser escalation: render, read, work hidden controls, read the
    screenshot if text still falls short, then measure field boxes."""
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

    allowed = set(state.get("allowed_domains") or [registrable_domain(state["root_domain"])])
    links = extract_link_contexts(rendered.html, rendered.final_url, allowed_domains=allowed)
    text = html_to_model_text(rendered.html)
    folded = fold(text + "\n" + (rendered.text or ""))
    regex_people = extract_people(rendered.html, page_title=rendered.title, url=url)
    reading = await read_page(url=url, title=rendered.title, text=text, meter=deps.meter)
    people = (
        combine_with_regex(reading.people, regex_people, folded) if reading.ok else regex_people
    )
    people = merge_people(people, outcome.people)

    # Tabs, accordions, "load more": the model picks from the real
    # accessibility controls, never pixel coordinates.
    if rendered.controls and (
        reading.hidden_content or reading.looks_incomplete or not reading.ok
    ):
        decision = await decide_navigation(
            url=rendered.final_url, title=rendered.title, text=rendered.text,
            links=rendered.links, controls=rendered.controls,
            screenshot=rendered.screenshot, meter=deps.meter,
        )
        if decision.reason:
            log.info("navigation for %s: %s (%s)", url, decision.page_type, decision.reason)
        for control in decision.controls:
            log.info("activating %s %r on %s", control["role"], control["name"], url)
            after = await click_by_accessible_name(
                deps.browser_context, url, control["role"], control["name"]
            )
            if not after.ok:
                continue
            after_text = html_to_model_text(after.html)
            after_reading = await read_page(
                url=url, title=after.title, text=after_text, meter=deps.meter
            )
            after_regex = extract_people(after.html, page_title=after.title, url=url)
            people = merge_people(
                people,
                combine_with_regex(after_reading.people, after_regex, fold(after_text))
                if after_reading.ok else after_regex,
            )
            links = _merge_links(
                links, extract_link_contexts(after.html, after.final_url, allowed_domains=allowed)
            )

    # Contacts published as images, or layout that carries the meaning: read
    # the screenshot itself when the text still falls short of what the page
    # shows.
    expected = max(
        reading.expected_people_count,
        outcome.reading.expected_people_count if outcome.reading else 0,
    )
    if rendered.screenshot and (not people or len(people) < expected * 0.75):
        vision = await read_page(
            url=url, title=rendered.title, text=rendered.text or text,
            screenshot=rendered.screenshot, meter=deps.meter,
        )
        if vision.ok:
            people = merge_people(people, vision.people)

    field_locations = rendered.field_locations
    hints = [h for person in people for h in person.locate_hints]
    if hints and not field_locations:
        located = await render_page(
            deps.browser_context, url, capture_screenshot=False, locate=hints
        )
        field_locations = located.field_locations if located.ok else {}

    if rendered.screenshot:
        path = save_screenshot(
            rendered.screenshot,
            site_run_id=state["site_run_id"],
            url_hash=url_hash(url),
        )
        outcome.screenshot_rel = relative_path(path)
        # Needed to place the field boxes, which are in screenshot pixels.
        outcome.shot_size = png_dimensions(rendered.screenshot)

    outcome.people = people
    outcome.fetch_mode = FetchMode.BOTH
    outcome.field_locations = field_locations
    outcome.title = rendered.title or outcome.title
    if reading.ok and (
        outcome.reading is None
        or not outcome.reading.ok
        or len(reading.people) >= len(outcome.reading.people)
    ):
        outcome.reading = reading
    return _Rendered(links=links)


def _update_programs(
    programs: list[dict], candidate: dict, outcome: PageOutcome, trainees: int
) -> None:
    """Attribute the page to a program and mark the program covered once a
    current trainee roster with people on it has been read."""
    if not programs or outcome.duplicate:
        return
    reading = outcome.reading
    program = match_program(
        programs,
        (reading.program if reading and reading.program else None) or candidate.get("program"),
        outcome.url,
    )
    if program is None:
        return
    program["pages"] = program.get("pages", 0) + 1
    visits = program.setdefault("visited", [])
    if len(visits) < _PROGRAM_VISITS_KEPT:
        visits.append({"url": outcome.url, "records": len(outcome.people), "trainees": trainees})
    if trainees and (reading is None or reading.is_current_trainee_roster or trainees >= 3):
        program["people"] = program.get("people", 0) + trainees
        if program.get("status") == PENDING:
            program["status"] = FOUND
            log.info("program covered: %s (%d trainees on %s)", program["name"], trainees, outcome.url)


def pending_program_names(programs: list[dict]) -> list[str]:
    return [p["name"] for p in programs if p.get("status") == PENDING]


async def _grow_frontier(
    deps: PipelineDeps,
    state: SiteState,
    candidates: list[dict],
    cursor: int,
    links: dict[str, dict],
    *,
    allowed: set[str],
    triaged: set[str],
    programs: list[dict],
) -> tuple[list[dict], set[str]]:
    """Send links not yet considered to the model, and queue what it keeps."""
    known = {link_key(c["url"]) for c in candidates}
    fresh: list[dict] = []
    for url, link in links.items():
        canonical = canonicalize(url)
        if not canonical or not in_scope(host_of(canonical), allowed):
            continue
        key = link_key(canonical)
        if key in triaged or key in known:
            continue
        triaged.add(key)
        fresh.append({**link, "url": canonical})
    if not fresh:
        return candidates, triaged

    pending = pending_program_names(programs)
    context = (
        "Programs still missing a roster: " + "; ".join(pending[:80]) if pending else ""
    )
    decisions = await triage_links(
        fresh, source=f"pages on {state['root_domain']}", context=context, meter=deps.meter,
    )
    additions = [
        {
            "url": d.url, "score": d.heuristic, "priority": d.priority,
            "program": d.program, "is_known_path": False,
        }
        for d in decisions
        if not d.skipped and not _is_asset(d.heuristic)
    ]
    return merge_frontier(candidates, cursor, additions), triaged


def _is_asset(heuristic: float) -> bool:
    # score_url marks non-HTML files and unfetchable URLs with -100.
    return heuristic <= -100


def merge_frontier(candidates: list[dict], cursor: int, additions: list[dict]) -> list[dict]:
    """Fold new candidates into the unvisited tail and re-sort it by priority.

    Only the tail is touched, so the caller's cursor stays valid. Everything
    already visited keeps its place.
    """
    if not additions:
        return candidates
    head, tail = candidates[:cursor], candidates[cursor:]
    by_key = {link_key(c["url"]): c for c in tail}
    visited = {link_key(c["url"]) for c in head}
    for addition in additions:
        key = link_key(addition["url"])
        if key in visited:
            continue
        existing = by_key.get(key)
        if existing is None or addition.get("priority", 0) > existing.get("priority", 0):
            by_key[key] = {
                **(existing or {}),
                **{k: v for k, v in addition.items() if v is not None},
            }
    merged = sorted(
        by_key.values(),
        key=lambda c: (-float(c.get("priority", 0.0)), -float(c.get("score", 0.0))),
    )
    room = max(settings.max_candidates - len(head), 0)
    if len(merged) > room:
        log.info("frontier at capacity; dropping %d lowest-priority links", len(merged) - room)
        merged = merged[:room]
    log.info(
        "frontier: %d links added, %d unvisited (top priority %.0f)",
        len(additions), len(merged), merged[0].get("priority", 0) if merged else 0,
    )
    return [*head, *merged]


def _title_of(html: str) -> str:
    from selectolax.parser import HTMLParser

    node = HTMLParser(html).css_first("title")
    return node.text().strip() if node else ""


def host_for(url: str) -> str:
    return host_of(url)
