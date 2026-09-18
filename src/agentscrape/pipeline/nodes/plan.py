"""Program planning and gap filling around the extract loop.

    discover -> plan -> extract (loop) -> gap_fill -> extract ... -> finalize

`plan` reads the entry page (and the program lists it points to) into a list of
the institution's residency and fellowship programs, and queues each program's
own page near the front of the work list. `extract` marks a program covered
once it reads a current trainee roster for it. `gap_fill` runs when the work
list stops producing: for every program still uncovered, the model is shown what
was visited and what is queued, and names the pages to try next.
"""

from __future__ import annotations

import asyncio
import logging

from ...config import settings
from ...discovery.sitemap import extract_link_contexts
from ...extraction.text import html_to_model_text
from ...llm.planner import (
    NOT_PUBLISHED,
    PENDING,
    match_program,
    merge_programs,
    read_program_list,
    suggest_for_gap,
)
from ...urls import canonicalize, host_of, registrable_domain
from ..deps import PipelineDeps
from ..state import SiteState
from .extract import link_key, merge_frontier

log = logging.getLogger("agentscrape.pipeline.plan")

# Program-list pages read per site, entry page included.
MAX_PROGRAM_PAGES = 30
PROGRAM_PRIORITY = 92.0
GAP_PRIORITY = 97.0
MAX_GAP_ROUNDS = 2
MAX_GAP_PROGRAMS = 80


async def _page(deps: PipelineDeps, url: str) -> tuple[str, str, str, str] | None:
    """(final url, title, model text, html) for a page, rendering it when the HTML is a shell."""
    from .extract import _title_of

    result = await deps.fetcher.get(url, attempts=2)
    html, final, title = "", url, ""
    if result.ok and result.is_html:
        html, final, title = result.text, result.final_url or url, _title_of(result.text)
    text = html_to_model_text(html)
    if len(text) < 1_500 and deps.can_render:
        from ...browser.renderer import render_page

        try:
            rendered = await asyncio.wait_for(
                render_page(deps.browser_context, url, capture_screenshot=False),
                timeout=settings.page_timeout_seconds + 15,
            )
        except TimeoutError:
            rendered = None
        if rendered is not None and rendered.ok:
            html, final, title = rendered.html, rendered.final_url, rendered.title
            text = html_to_model_text(html)
    if not html:
        return None
    return final, title, text, html


async def plan_programs(state: SiteState, deps: PipelineDeps) -> SiteState:
    allowed = set(state.get("allowed_domains") or [registrable_domain(state["root_domain"])])
    programs: list[dict] = [dict(p) for p in state.get("programs", [])]
    to_read = [state["root_url"]]
    read: set[str] = set()

    while to_read and len(read) < MAX_PROGRAM_PAGES:
        url = to_read.pop(0)
        key = link_key(url)
        if key in read:
            continue
        read.add(key)
        page = await _page(deps, url)
        if page is None:
            continue
        final, title, text, html = page
        links = [
            link.as_dict()
            for link in extract_link_contexts(html, final, allowed_domains=allowed)
        ]
        found, more = await read_program_list(
            url=final, title=title, text=text, links=links, meter=deps.meter,
        )
        programs = merge_programs(programs, found)
        for next_url in more:
            if link_key(next_url) not in read and next_url not in to_read:
                to_read.append(next_url)

    log.info(
        "%s: %d programs planned (%d with a landing page) from %d program-list pages",
        state["root_domain"], len(programs),
        sum(1 for p in programs if p.get("landing_url")), len(read),
    )
    additions = [
        {
            "url": p["landing_url"], "score": 0.0, "priority": PROGRAM_PRIORITY,
            "program": p["name"], "is_known_path": False,
        }
        for p in programs
        if p.get("landing_url")
    ]
    candidates = merge_frontier(state.get("candidates", []), state.get("cursor", 0), additions)
    triaged = set(state.get("triaged", []))
    triaged.update(link_key(a["url"]) for a in additions)
    if programs:
        await deps.note(
            state,
            f"Identified {len(programs)} residency and fellowship programs to cover",
            programs=len(programs),
        )
    return {**state, "programs": programs, "candidates": candidates, "triaged": sorted(triaged)}


def pending_programs(state: SiteState) -> list[dict]:
    return [p for p in state.get("programs", []) if p.get("status") == PENDING]


async def gap_fill(state: SiteState, deps: PipelineDeps) -> SiteState:
    """Ask the model where each still-uncovered program's roster might be."""
    allowed = set(state.get("allowed_domains") or [registrable_domain(state["root_domain"])])
    programs = [dict(p) for p in state.get("programs", [])]
    pending = [p for p in programs if p.get("status") == PENDING][:MAX_GAP_PROGRAMS]
    candidates = state.get("candidates", [])
    cursor = state.get("cursor", 0)
    tail = candidates[cursor:]
    visited_keys = {link_key(c["url"]) for c in candidates[:cursor]}
    hosts = sorted({host_of(c["url"]) for c in candidates if host_of(c["url"])})

    async def for_program(program: dict) -> list[str]:
        related = [
            c["url"] for c in tail
            if (c.get("program") and match_program([program], c["program"]) is not None)
            or match_program([program], None, c["url"]) is not None
        ]
        if len(related) < 250:
            related += [c["url"] for c in tail[: 250 - len(related)] if c["url"] not in related]
        urls, publishes = await suggest_for_gap(
            program=program,
            visited=program.get("visited", []),
            candidates=related,
            hosts=hosts,
            allowed=allowed,
            meter=deps.meter,
        )
        if publishes is False and program.get("pages", 0) > 0 and not urls:
            program["status"] = NOT_PUBLISHED
            log.info("program %s: model concludes no roster is published", program["name"])
        return urls

    suggestions = await asyncio.gather(*(for_program(p) for p in pending))
    additions = []
    for program, urls in zip(pending, suggestions, strict=True):
        for url in urls:
            canonical = canonicalize(url)
            if canonical and link_key(canonical) not in visited_keys:
                additions.append({
                    "url": canonical, "score": 0.0, "priority": GAP_PRIORITY,
                    "program": program["name"], "is_known_path": False,
                })
    rounds = state.get("gap_rounds", 0) + 1
    log.info(
        "%s gap fill round %d: %d programs pending, %d pages suggested",
        state["root_domain"], rounds, len(pending), len(additions),
    )
    triaged = set(state.get("triaged", []))
    triaged.update(link_key(a["url"]) for a in additions)
    await deps.note(
        state,
        f"{len(pending)} programs still have no roster; the agent suggested "
        f"{len(additions)} more pages to check",
    )
    return {
        **state,
        "programs": programs,
        "candidates": merge_frontier(candidates, cursor, additions),
        "triaged": sorted(triaged),
        "gap_rounds": rounds,
        # Fresh leads deserve a fresh run before the barren stop applies.
        "barren_streak": 0,
    }
