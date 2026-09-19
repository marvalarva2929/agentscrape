"""Hybrid strategy, stage 3b: map the site from plain HTML before the model reads anything.

    discover -> html_map -> plan -> extract (loop) ...

Navigation is the cheap part of a crawl and does not need a model: following
links, ranking them by the keyword heuristic, and noticing which pages carry
people are all plain HTML work. This pass fetches up to `html_map_max_pages`
pages best-first by heuristic score, follows the links it finds, and keeps a
**people signal** for each page. The model then reads only the pages with a
signal, ranked by how strong it is; mapped pages without one sit below the
work list's floor, where only gap filling can bring them back.

The signal is deliberately generous (recall first): a page is passed on if the
HTML extractor found three or more people, it carries two or more addresses, it
looks like a client-rendered or image-only roster, or its URL and title look
like a roster.

Measured on 2026-09-18: on the 195 UChicago pages an agent crawl took 948
residents and fellows from, this rule drops one page holding one trainee; on
322 random sitemap pages from Arizona and BCM it passes 34% to the model. "Any
person" instead passed 99% — the HTML extractor finds a name or two in almost
every footer, byline and contact block.
Pages past the cap are not dropped: they stay on the work list at their
heuristic priority and are gated by the same signal when fetched.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from dataclasses import dataclass

from ...config import settings
from ...discovery.scoring import score_url
from ...discovery.sitemap import extract_link_contexts
from ...domain.matching import EMAIL_RE
from ...extraction.html_people import extract_people, page_looks_thin
from ...extraction.text import html_to_model_text
from ...llm.triage import heuristic_priority
from ...urls import canonicalize, host_of, in_scope, registrable_domain
from ..deps import PipelineDeps
from ..state import SiteState
from .extract import _title_of, link_key, merge_frontier

log = logging.getLogger("agentscrape.pipeline.html_map")

MAP_BATCH = 24
# One or two names is a footer, a byline or a contact block; a roster has more.
MIN_PEOPLE = 3
MIN_EMAILS = 2
# score_url at or above this reads as a roster, directory or people page
# ("current residents" ~22, "surgery/residency" ~12, "directory" ~13).
ROSTER_SCORE = 10.0
# Where mapped pages with a signal land: above every unmapped page (heuristic
# priority tops out at 60) and below program landing pages (92) and gap-fill
# suggestions (97), which the planner chose deliberately.
SIGNAL_BASE = 62.0
SIGNAL_TOP = 90.0
# Mapped pages with no signal: below PRIORITY_FLOOR, so never read unless
# gap filling asks for them.
NO_SIGNAL_PRIORITY = 1.0
# Unmapped pages rank below every mapped page with a signal.
UNMAPPED_CAP = 55.0


@dataclass(frozen=True)
class PeopleSignal:
    people: int
    emails: int
    thin: bool
    score: float

    @property
    def keep(self) -> bool:
        return (
            self.people >= MIN_PEOPLE
            or self.emails >= MIN_EMAILS
            or self.thin
            or self.score >= ROSTER_SCORE
        )

    @property
    def priority(self) -> float:
        strength = self.people + self.emails / 2 + max(self.score, 0.0) / 2
        return min(SIGNAL_TOP, SIGNAL_BASE + strength)


def people_signal(html: str, url: str, title: str) -> PeopleSignal:
    """What the HTML alone says about whether a page lists people."""
    people = len(extract_people(html, page_title=title, url=url))
    emails = len({m.lower() for m in EMAIL_RE.findall(html or "")})
    thin, _ = page_looks_thin(html, html_to_model_text(html), people)
    return PeopleSignal(people, emails, thin, score_url(url, title=title or None).score)


async def html_map(state: SiteState, deps: PipelineDeps) -> SiteState:
    allowed = set(state.get("allowed_domains") or [registrable_domain(state["root_domain"])])
    candidates = state.get("candidates", [])
    deadline = time.monotonic() + settings.html_map_timeout_seconds
    cache_budget = settings.html_map_cache_mb * 1_000_000
    cached_bytes = 0

    # Best-first by heuristic score. Known paths first: they yielded before.
    queue: list[tuple[float, int, str]] = []
    queued: dict[str, dict] = {}
    order = 0

    def push(entry: dict, score: float) -> None:
        nonlocal order
        key = link_key(entry["url"])
        if key in queued:
            return
        queued[key] = entry
        rank = 1_000.0 if entry.get("is_known_path") else score
        heapq.heappush(queue, (-rank, order, key))
        order += 1

    for candidate in candidates:
        push(dict(candidate), float(candidate.get("score", 0.0)))

    await deps.note(state, f"Mapping {state['root_domain']} from plain HTML before the agent reads anything")

    mapped: dict[str, PeopleSignal | None] = {}
    fetched = 0
    while queue and fetched < settings.html_map_max_pages and time.monotonic() < deadline:
        if deps.stop_requested():
            break
        batch_keys = [
            heapq.heappop(queue)[2]
            for _ in range(min(MAP_BATCH, len(queue), settings.html_map_max_pages - fetched))
        ]
        entries = [queued[k] for k in batch_keys]
        results = await deps.fetcher.get_many([e["url"] for e in entries], attempts=2)
        fetched += len(entries)

        for key, entry, result in zip(batch_keys, entries, results, strict=True):
            if not (result.ok and result.is_html):
                # Could not map it; the model phase will try it on its merits.
                continue
            url = result.final_url or entry["url"]
            title = _title_of(result.text)
            signal = await asyncio.to_thread(people_signal, result.text, url, title)
            mapped[key] = signal
            if signal.keep and cached_bytes < cache_budget:
                deps.page_cache[entry["url"]] = result
                cached_bytes += len(result.text)
            for link in extract_link_contexts(result.text, url, allowed_domains=allowed):
                canonical = canonicalize(link.url)
                if not canonical or not in_scope(host_of(canonical), allowed):
                    continue
                score = score_url(canonical, title=link.text or None).score
                if score <= -100:
                    continue
                push(
                    {"url": canonical, "score": score, "priority": heuristic_priority(score),
                     "program": None, "is_known_path": False, "link_text": link.text},
                    score,
                )

    work: list[dict] = []
    for key, entry in queued.items():
        signal = mapped.get(key)
        base = {k: v for k, v in entry.items() if k != "link_text"}
        if signal is None:
            priority = min(float(entry.get("priority") or heuristic_priority(entry.get("score", 0.0))), UNMAPPED_CAP)
            if entry.get("is_known_path"):
                priority = max(priority, SIGNAL_TOP)
            work.append({**base, "priority": priority, "mapped": False})
        elif signal.keep or entry.get("is_known_path"):
            work.append({**base, "priority": max(signal.priority, SIGNAL_TOP if entry.get("is_known_path") else 0.0),
                         "mapped": True, "signal": True})
        else:
            work.append({**base, "priority": NO_SIGNAL_PRIORITY, "mapped": True, "signal": False})

    kept = sum(1 for w in work if w.get("signal"))
    stats = {"pages_mapped": len(mapped), "pages_with_people": kept, "urls_known": len(queued)}
    log.info(
        "html map for %s: %d fetched, %d mapped, %d with a people signal, %d urls known",
        state["root_domain"], fetched, len(mapped), kept, len(queued),
    )
    await deps.note(
        state,
        f"Mapped {len(mapped):,} pages without the model; {kept:,} show people and go to the agent",
        pages_mapped=len(mapped), pages_with_people=kept,
    )
    triaged = set(state.get("triaged", []))
    triaged.update(queued)
    return {
        **state,
        "candidates": merge_frontier([], 0, work),
        "candidates_considered": max(state.get("candidates_considered", 0), len(queued)),
        "triaged": sorted(triaged),
        "cursor": 0,
        "html_map_stats": stats,
    }
