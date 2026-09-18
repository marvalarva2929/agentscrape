"""Program-level planning: what programs exist, which are covered, what is left.

Without this the crawl had no notion of done. It stopped after a run of barren
pages, and it could not tell a site with every roster collected from one that
had spent its budget in a patient-facing physician finder. With a program list
the crawl knows what it is looking for, favours links toward programs still
missing, and asks the model where a missing roster might be before it gives up.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from ..config import settings
from ..urls import canonicalize, host_of, in_scope
from .prompts import GAP_FILL_SYSTEM, PROGRAMS_SYSTEM
from .provider import VisionProvider, get_provider
from .usage import LLMUnavailable, UsageMeter

log = logging.getLogger("agentscrape.llm.planner")

PENDING = "pending"
FOUND = "roster_found"
NOT_PUBLISHED = "no_roster_published"

_FILLER = frozenset({
    "residency", "residencies", "fellowship", "fellowships", "program", "programs",
    "programme", "department", "of", "the", "and", "in", "at", "for", "training",
    "division", "section", "medicine" , "&",
})
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(name: str) -> set[str]:
    value = unicodedata.normalize("NFKD", name or "")
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    words = set(_TOKEN.findall(value))
    core = words - _FILLER
    # "Internal Medicine Residency" must not reduce to {"internal"} and then
    # match nothing else; keep "medicine" when it is all there is.
    return core or words


def match_program(programs: list[dict], name: str | None, url: str | None = None) -> dict | None:
    """The program a page belongs to, by landing-URL prefix, then by name."""
    if url:
        best = None
        for program in programs:
            landing = program.get("landing_url")
            if landing:
                prefix = landing.rstrip("/")
                if (url == landing or url.startswith(prefix + "/")) and (
                    best is None or len(landing) > len(best["landing_url"])
                ):
                    best = program
        if best is not None:
            return best
    if not name:
        return None
    wanted = _tokens(name)
    if not wanted:
        return None
    best, best_score = None, 0.0
    for program in programs:
        have = _tokens(program["name"])
        if not have:
            continue
        overlap = len(wanted & have)
        score = overlap / len(wanted | have)
        if have <= wanted or wanted <= have:
            score = max(score, 0.75)
        # The type has to agree when both sides state it, or "Pediatrics
        # Residency" swallows every pediatric fellowship.
        if program.get("kind") in ("residency", "fellowship"):
            other = "fellowship" if program["kind"] == "residency" else "residency"
            if other in (name or "").lower() and program["kind"] not in (name or "").lower():
                score *= 0.5
        if score > best_score:
            best, best_score = program, score
    return best if best_score >= 0.6 else None


async def _call(
    provider: VisionProvider, system: str, user: str, meter: UsageMeter | None, what: str
) -> dict | None:
    try:
        response = await provider.complete(
            system=system, user=user, meter=meter, model=settings.text_model,
            max_tokens=8_000,
        )
    except LLMUnavailable:
        raise
    except Exception as exc:
        log.warning("%s failed: %s", what, exc)
        if meter is not None:
            meter.note_failure(what, exc)
        return None
    payload = response.json()
    if not isinstance(payload, dict):
        log.warning("%s returned unparseable output", what)
        return None
    return payload


async def read_program_list(
    *,
    url: str,
    title: str,
    text: str,
    links: list[dict],
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
) -> tuple[list[dict], list[str]]:
    """Programs named on a page, and links to further program lists."""
    provider = provider or get_provider()
    links = links[:600]
    numbered = "\n".join(
        f"{i}. {link['url']} | {link.get('text', '')}" for i, link in enumerate(links)
    )
    payload = await _call(
        provider, PROGRAMS_SYSTEM,
        f"URL: {url}\nTitle: {title}\n\nPage text:\n---\n{text[:60_000]}\n---\n\nLinks:\n{numbered}",
        meter, f"program list ({url})",
    )
    if not payload:
        return [], []

    def link_at(index: Any) -> str | None:
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(links):
            return links[index]["url"]
        return None

    programs = []
    for item in payload.get("programs") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"].strip()[:200]
        if not name:
            continue
        kind = str(item.get("kind") or "other").lower()
        programs.append({
            "name": name,
            "kind": kind if kind in ("residency", "fellowship") else "other",
            "landing_url": link_at(item.get("link")),
            "status": PENDING,
            "people": 0,
            "pages": 0,
        })
    more = [u for u in (link_at(i) for i in payload.get("more_program_lists") or []) if u]
    log.info("program list %s: %d programs, %d further lists", url, len(programs), len(more))
    return programs, more


def merge_programs(existing: list[dict], new: list[dict]) -> list[dict]:
    out = list(existing)
    for program in new:
        match = match_program(out, program["name"])
        if match is not None and _tokens(match["name"]) == _tokens(program["name"]):
            if not match.get("landing_url") and program.get("landing_url"):
                match["landing_url"] = program["landing_url"]
            continue
        out.append(program)
    return out


async def suggest_for_gap(
    *,
    program: dict,
    visited: list[dict],
    candidates: list[str],
    hosts: list[str],
    allowed: set[str],
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
) -> tuple[list[str], bool | None]:
    """URLs worth trying for a program with no roster yet, and whether the model
    concluded it does not publish one."""
    provider = provider or get_provider()
    visited_lines = "\n".join(
        f"- {v['url']} (people found: {v.get('records', 0)})" for v in visited[:60]
    ) or "(none)"
    candidate_lines = "\n".join(f"- {u}" for u in candidates[:250]) or "(none)"
    payload = await _call(
        provider, GAP_FILL_SYSTEM,
        (
            f"Program: {program['name']} ({program.get('kind', 'other')})\n"
            f"Program page: {program.get('landing_url') or 'unknown'}\n\n"
            f"Visited pages for this program:\n{visited_lines}\n\n"
            f"Unvisited candidate URLs:\n{candidate_lines}\n\n"
            f"Known hostnames: {', '.join(hosts[:150])}"
        ),
        meter, f"gap fill ({program['name']})",
    )
    if not payload:
        return [], None
    urls: list[str] = []
    known_hosts = set(hosts)
    for raw in payload.get("urls") or []:
        canonical = canonicalize(raw) if isinstance(raw, str) else None
        # Guessed paths are fine; guessed hostnames are not.
        if (
            canonical
            and host_of(canonical) in known_hosts
            and in_scope(host_of(canonical), allowed)
            and canonical not in urls
        ):
            urls.append(canonical)
    publishes = payload.get("publishes_roster")
    return urls[:10], publishes if isinstance(publishes, bool) else None
