"""Stage 4 and 5: work the frontier, with the model reading every page.

For each page:
  1. plain HTML fetch
  2. the model reads the page text (hidden tabs, mailto addresses, alt text and
     embedded JSON included) and says who is on it, what kind of page it is,
     and whether people are hidden or missing
  3. escalate to a rendered page when the model says the people are not in the
     HTML, or it counts more people than it could read; activate up to five
     tabs / "load more" controls it picks from the accessibility tree
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
from ...db.repositories.records import (
    ExtractionContext,
    lock_site,
    reconcile_people,
    run_counts,
)
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
from ...orchestrator.limits import SiteCounts
from ...urls import canonicalize, host_of, in_scope, registrable_domain, url_hash
from ..browser_fetch import browser_fetch
from ..checkpoint import save_checkpoint
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.extract")

# Pages per loop iteration. Fetches, model reads and (bounded) browser work in a
# batch all run concurrently.
BATCH_SIZE = 32
# This many HTTP 403 refusals in a row, with no page served between them,
# means the site has blocked the crawler.
BLOCKED_AFTER_REFUSALS = 10
_TRAINEES = (PersonCategory.RESIDENT, PersonCategory.FELLOW)
# How many visited pages to remember per program for gap filling.
_PROGRAM_VISITS_KEPT = 60


@dataclass
class PageOutcome:
    url: str
    people: list[ExtractedPerson] = field(default_factory=list)
    fetch_mode: FetchMode = FetchMode.HTML
    title: str = ""
    links: list[LinkContext] = field(default_factory=list)
    reading: PageReading | None = None
    steps: int = 1
    duplicate: bool = False
    # Hybrid strategy: the HTML showed no sign of people, so the model was
    # not asked to read the page.
    gated: bool = False


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

    urls = [c["url"] for c in claimed]
    results = await _fetch(deps, urls)
    refusals = state.get("http_refusals", 0)
    # Plain requests are being turned away: try the same page as a person would
    # open it. If the browser gets in, everything from here is read in it. A 429
    # is never worked around; it is a request to slow down.
    if (
        deps.can_render and not deps.browser_only
        and not any(result.status == 429 for result in results)
        and refusals + sum(result.status == 403 for result in results) >= BLOCKED_AFTER_REFUSALS
    ):
        probe = next(result.url for result in results if result.status == 403)
        if (await browser_fetch(deps, probe)).ok:
            deps.browser_only = True
            refusals = 0
            await deps.note(
                state,
                "The site refuses plain requests but lets a browser in, so its pages are read in a browser.",
            )
            results = await _fetch(deps, urls)
    rate_limited = sum(result.status == 429 for result in results)
    # Refusals in a row, carried across batches. A university has protected
    # pages all over (UChicago returned 403 on 20 scattered department pages
    # in half an hour while serving hundreds of others), so a running total
    # stopped healthy crawls; a site that has blocked the crawler refuses
    # everything, one request after another.
    for result in results:
        if result.status == 403:
            refusals += 1
        elif result.ok:
            refusals = 0
    # A 429 is an explicit request to stop. Do not spend the rest of the page
    # budget or escalate to the browser in either case.
    if rate_limited or refusals >= BLOCKED_AFTER_REFUSALS:
        code = "SITE_RATE_LIMITED" if rate_limited else "SITE_BLOCKED"
        message = (
            "The site rate-limited this crawl (HTTP 429). "
            "This site could not be crawled; try again later."
            if rate_limited
            else "The site repeatedly blocked automated requests (HTTP 403). "
            "This site could not be crawled; try again later."
        )
        await deps.note(state, message)
        return {
            **state,
            "http_refusals": refusals,
            "status": "failed",
            "terminated": True,
            "error_code": code,
            "error_message": message,
        }
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
    # A dict used as an ordered set. The same person is seen on many pages —
    # their roster, their department directory, the institution-wide one — and
    # appending every sighting grew BCM's list to 29,164 ids for about 1,750
    # people. The list is rewritten into the checkpoint after every batch, so
    # that growth made the checkpoint 1.8MB and the write cost quadratic in the
    # length of the crawl. `finalize` only ever reads it as a set.
    seen_ids = dict.fromkeys(state.get("seen_record_ids", []))
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
            seen_ids.update(dict.fromkeys(reconciled.record_ids))
            records_here = reconciled.total_seen

            if candidate.get("is_known_path") and records_here:
                known_hits += 1
                await deps.emitter.emit(
                    EventType.KNOWN_PATH_HIT,
                    site_id=state["site_id"], site_run_id=state["site_run_id"],
                    agent_id=state.get("agent_id"), domain=state.get("root_domain"),
                    url=url, records=records_here,
                )

        covered = _update_programs(programs, candidate, outcome, trainees_here)
        if covered:
            done = sum(1 for p in programs if p.get("status") != PENDING)
            await deps.note(
                state,
                f"Roster found for {covered} ({trainees_here} residents/fellows) "
                f"\u2014 {done} of {len(programs)} programs covered",
                program=covered,
            )

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

        if outcome.duplicate:
            continue
        await deps.emitter.emit(
            EventType.SITE_STEP,
            site_id=state["site_id"],
            site_run_id=state["site_run_id"],
            # Who did it, so the monitor can say what each agent is reading.
            agent_id=state.get("agent_id"),
            domain=state.get("root_domain"),
            url=url,
            action=f"fetch:{outcome.fetch_mode}",
            records=records_here,
            trainees=trainees_here,
            title=outcome.title or None,
            page_type=outcome.reading.page_type if outcome.reading and outcome.reading.ok else None,
            program=outcome.reading.program if outcome.reading and outcome.reading.ok else None,
            message=_describe(outcome, records_here, trainees_here),
            steps_taken=steps_taken + steps_used,
            step_budget=state["step_budget"],
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
        "seen_record_ids": list(seen_ids),
        "known_path_hits": known_hits,
        "barren_streak": barren_streak,
        "http_refusals": refusals,
        "programs": programs,
        "triaged": sorted(triaged),
        "processed_hashes": sorted(processed_hashes),
    }

    # Checkpoint after every batch, so an interrupted site resumes here instead
    # of re-running discovery, which is the expensive stage.
    async with deps.sessionmaker() as session:
        await save_checkpoint(session, updated)
        await session.commit()
        # Running totals for the run's people / residents & fellows / email
        # limits, so they trip mid-crawl rather than when the school finishes.
        people, trainees, emails = await run_counts(
            session, state["site_id"], state.get("run_id")
        )
    await deps.report_counts(SiteCounts(people, trainees, emails))

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
        if _gate_applies(state, candidate):
            from .html_map import people_signal

            signal = await asyncio.to_thread(people_signal, result.text, url, outcome.title)
            if not signal.keep:
                outcome.gated = True
                return outcome
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


async def _fetch(deps: PipelineDeps, urls: list[str]):
    """Fetch results in order, reusing bodies the HTML pass already has."""
    cached = {url: deps.page_cache.pop(url) for url in urls if url in deps.page_cache}
    missing = [url for url in urls if url not in cached]
    if deps.browser_only and missing:
        lock = _render_lock(deps)

        async def one(url: str):
            async with lock:
                return await browser_fetch(deps, url)

        fetched = dict(zip(missing, await asyncio.gather(*(one(url) for url in missing)), strict=True))
    else:
        fetched = dict(zip(missing, await deps.fetcher.get_many(missing), strict=True)) if missing else {}
    return [cached.get(url) or fetched[url] for url in urls]


def _gate_applies(state: SiteState, candidate: dict) -> bool:
    """Whether the page must show people in its HTML before the model reads it.

    Only on the hybrid strategy, and never for a page the HTML pass already
    vouched for or one the planner or gap filling chose on purpose (they queue
    at priority 92 and above).
    """
    if state.get("crawl_strategy") != "hybrid" or candidate.get("signal"):
        return False
    from .html_map import SIGNAL_TOP

    return float(candidate.get("priority", 0.0)) < SIGNAL_TOP


# Browser pages open at once in the site's context. Renders were the
# bottleneck when serialized: a render plus tab clicks runs 15-60 seconds.
RENDER_CONCURRENCY = 4


def _describe(outcome: PageOutcome, records: int, trainees: int) -> str:
    """One line for the live feed: what the agent made of the page."""
    name = outcome.title or outcome.url
    if outcome.gated:
        return f"skipped \u201c{name}\u201d: no people in its HTML"
    reading = outcome.reading
    kind = reading.page_type if reading and reading.ok else "page"
    if records:
        who = f"{records} people" + (f", {trainees} residents/fellows" if trainees else "")
        verb = "rendered and read" if outcome.fetch_mode == FetchMode.BOTH else "read"
        return f"{verb} {kind} \u201c{name}\u201d: {who}"
    return f"checked {kind} \u201c{name}\u201d: nobody listed"


def _render_lock(deps: PipelineDeps) -> asyncio.Semaphore:
    """Bounds concurrent browser pages within the site's one browser context."""
    lock = getattr(deps, "_render_lock", None)
    if lock is None:
        lock = asyncio.Semaphore(RENDER_CONCURRENCY)
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
    """Browser escalation: render, read, and work hidden controls."""
    from ...browser.renderer import click_by_accessible_name, render_page

    try:
        rendered = await asyncio.wait_for(
            render_page(deps.browser_context, url),
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
            links=rendered.links, controls=rendered.controls, meter=deps.meter,
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

    outcome.people = people
    outcome.fetch_mode = FetchMode.BOTH
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
) -> str | None:
    """Attribute the page to a program and mark the program covered once a
    current trainee roster with people on it has been read. Returns the
    program's name when this page is what covered it."""
    if not programs or outcome.duplicate:
        return None
    reading = outcome.reading
    program = match_program(
        programs,
        (reading.program if reading and reading.program else None) or candidate.get("program"),
        outcome.url,
    )
    if program is None:
        return None
    program["pages"] = program.get("pages", 0) + 1
    visits = program.setdefault("visited", [])
    if len(visits) < _PROGRAM_VISITS_KEPT:
        visits.append({"url": outcome.url, "records": len(outcome.people), "trainees": trainees})
    # The model's call, not a head count: UChicago's internal medicine program
    # page lists its 4 chief residents, and treating that as the roster
    # marked a 122-resident program covered and sent its real roster page to
    # the back of the queue. The count only stands in when the model could not
    # read the page at all.
    model_read = reading is not None and reading.ok
    if trainees and (
        (model_read and reading.is_current_trainee_roster)
        or (not model_read and trainees >= 3)
    ):
        program["people"] = program.get("people", 0) + trainees
        if program.get("status") == PENDING:
            program["status"] = FOUND
            log.info("program covered: %s (%d trainees on %s)", program["name"], trainees, outcome.url)
            return program["name"]
    return None


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
    pending = pending_order(programs)
    if not fresh:
        # Programs covered by this batch move their remaining pages back.
        return merge_frontier(candidates, cursor, [], pending if programs else None), triaged

    missing = pending_program_names(programs)
    context = (
        "Programs still missing a roster: " + "; ".join(missing[:80]) if missing else ""
    )
    decisions = await triage_links(
        fresh, source=f"pages on {state['root_domain']}", context=context, meter=deps.meter,
    )
    additions = attribute_programs([
        {
            "url": d.url, "score": d.heuristic, "priority": d.priority,
            "program": d.program, "is_known_path": False,
        }
        for d in decisions
        if not d.skipped and not _is_asset(d.heuristic)
    ], programs)
    return merge_frontier(candidates, cursor, additions, pending if programs else None), triaged


def _is_asset(heuristic: float) -> bool:
    # score_url marks non-HTML files and unfetchable URLs with -100.
    return heuristic <= -100


# Below this a page is not worth reading at all (graph.PRIORITY_FLOOR); such
# pages sort last whatever program they belong to.
_READ_FLOOR = 5.0


def attribute_programs(entries: list[dict], programs: list[dict]) -> list[dict]:
    """Tie each page to one of the planner's programs where it can be: under a
    program's landing page, or named for it by the model's triage. The page's
    `program` becomes that program's name, which is what `program_first`
    ranks on. Pages that match nothing are left as they are."""
    if not programs:
        return entries
    for entry in entries:
        program = match_program(programs, entry.get("program"), entry.get("url"))
        if program is not None:
            entry["program"] = program["name"]
    return entries


def pending_names(programs: list[dict]) -> set[str]:
    return {p["name"] for p in programs if p.get("status") == PENDING}


# Residencies before fellowships: a residency roster lists a whole program's
# classes (UChicago internal medicine: 122), a fellowship a handful (often 3).
# Reaching the big rosters first is most of the recall in the first half hour.
_KIND_ORDER = {"residency": 0, "other": 1, "fellowship": 2}


def pending_order(programs: list[dict]) -> dict[str, int]:
    """Programs still missing a roster, each with its place in the queue."""
    return {
        p["name"]: _KIND_ORDER.get(p.get("kind") or "other", 1)
        for p in programs if p.get("status") == PENDING
    }


def program_first(pending: dict[str, int] | set[str]):
    """Sort key: pages of programs still missing a roster first (residencies,
    then other programs, then fellowships), then everything else, then pages
    below the read floor; by priority within each.

    The goal of a crawl is the program list. Staff directories, department
    news and the university's business pages can hold people too, but they
    wait until every program the planner found has a roster or has been
    searched for, however many names they show."""
    order = pending if isinstance(pending, dict) else dict.fromkeys(pending, 0)

    def key(candidate: dict) -> tuple:
        priority = float(candidate.get("priority", 0.0))
        rank = 0
        if candidate.get("is_priority_input"):
            # A client-supplied link: examined first regardless of program
            # attribution, but still ranked and read like any other
            # candidate - a hint about order, not a different code path.
            tier = -1
        elif priority < _READ_FLOOR:
            tier = 2
        elif candidate.get("program") in order:
            tier, rank = 0, order[candidate["program"]]
        else:
            tier = 1
        return (tier, rank, -priority, -float(candidate.get("score", 0.0)))

    return key


def next_is_program_page(state: SiteState) -> bool:
    candidates = state.get("candidates", [])
    cursor = state.get("cursor", 0)
    return cursor < len(candidates) and (
        candidates[cursor].get("program") in pending_names(state.get("programs", []))
    )


def merge_frontier(
    candidates: list[dict], cursor: int, additions: list[dict],
    pending: dict[str, int] | set[str] | None = None,
) -> list[dict]:
    """Fold new candidates into the unvisited tail and re-sort it.

    The tail is ordered by `program_first` when the programs still pending are
    given, by priority otherwise. Only the tail is touched, so the caller's
    cursor stays valid. Everything already visited keeps its place.
    """
    if not additions and pending is None:
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
        key=program_first(pending) if pending is not None
        else (lambda c: (-float(c.get("priority", 0.0)), -float(c.get("score", 0.0)))),
    )
    room = max(settings.max_candidates - len(head), 0)
    if len(merged) > room:
        log.info("frontier at capacity; dropping %d lowest-priority links", len(merged) - room)
        merged = merged[:room]
    if additions:
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
