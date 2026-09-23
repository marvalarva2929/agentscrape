"""Role verification: re-reads one already-scraped page and confirms every
category it supports for the people the crawl found there.

Run only on demand against data already in the database (see
`agentscrape.verification.service`), never as part of a crawl. The same
groundedness discipline as `reader.py` applies: a role is kept only when it
is one of the known categories, and the person's name must still appear on
the page.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..config import settings
from ..db.enums import PersonCategory
from .prompts import VERIFY_ROLES_SYSTEM, verify_roles_user_prompt
from .provider import VisionProvider, get_provider
from .reader import fold, name_in_text
from .usage import LLMUnavailable, UsageMeter

log = logging.getLogger("agentscrape.llm.verify")

_ROLES = frozenset(c.value for c in PersonCategory if c != PersonCategory.UNKNOWN)


def _is_answer(payload: object) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("people"), list)


def _last_json_object(text: str) -> dict | None:
    """The model sometimes reasons past its own answer and appends a
    corrected object afterward ("...Correction: {...}"), which breaks a
    parser that spans from the first '{' to the last '}' - it swallows the
    prose in between. Scan for every brace-balanced {...} object in the text
    instead and return the last one that parses and has the right shape:
    the model's own final answer, not its first guess.
    """
    candidates: list[str] = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start : i + 1])
                start = None
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if _is_answer(parsed):
            return parsed
    return None


@dataclass(frozen=True)
class RoleCheckInput:
    record_id: str
    full_name: str
    category: str
    position: str | None = None


async def verify_page_roles(
    *,
    url: str,
    title: str,
    text: str,
    people: list[RoleCheckInput],
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
) -> dict[str, list[str]]:
    """Roles per record id, grounded against the page text.

    A record missing from the model's response, or whose response could not
    be grounded, is left out entirely - the caller keeps that record's
    existing category rather than treating "not returned" as "no roles".
    """
    if not people:
        return {}
    provider = provider or get_provider()
    by_name: dict[str, list[RoleCheckInput]] = {}
    for person in people:
        by_name.setdefault(person.full_name, []).append(person)

    prompt = verify_roles_user_prompt(
        url=url,
        title=title,
        text=text,
        people=[
            {"name": p.full_name, "category": p.category, "position": p.position}
            for p in people
        ],
    )
    # One retry on an unparseable response: usually the JSON was cut off or the
    # model added prose, not a reason to give up on the whole page.
    payload = None
    for attempt in range(2):
        try:
            response = await provider.complete(
                system=VERIFY_ROLES_SYSTEM, user=prompt, meter=meter, model=settings.text_model,
            )
            payload = response.json()
            if not _is_answer(payload):
                payload = _last_json_object(response.text)
        except LLMUnavailable:
            raise
        except Exception as exc:
            log.warning("role verification failed for %s: %s", url, exc)
            if meter is not None:
                meter.note_failure("verify_roles", exc)
            return {}
        if _is_answer(payload):
            break
        log.warning("role verification for %s returned unparseable output (attempt %d)", url, attempt + 1)
        payload = None
    if payload is None:
        if meter is not None:
            meter.note_failure("verify_roles", "unparseable output")
        return {}

    raw_people = payload.get("people")

    folded = fold(text)
    out: dict[str, list[str]] = {}
    for entry in raw_people:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or name not in by_name:
            continue
        roles_raw = entry.get("roles")
        if not isinstance(roles_raw, list):
            continue
        roles = sorted({
            str(r).strip().lower() for r in roles_raw if str(r).strip().lower() in _ROLES
        })
        if not roles:
            continue
        for candidate in by_name[name]:
            if not name_in_text(candidate.full_name, folded):
                continue
            out[candidate.record_id] = roles
    return out
