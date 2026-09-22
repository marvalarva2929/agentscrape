"""Role verification: re-reads one already-scraped page and confirms every
category it supports for the people the crawl found there.

Run only on demand against data already in the database (see
`agentscrape.verification.service`), never as part of a crawl. The same
groundedness discipline as `reader.py` applies: a role is kept only when it
is one of the known categories, and the person's name must still appear on
the page.
"""

from __future__ import annotations

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
    try:
        response = await provider.complete(
            system=VERIFY_ROLES_SYSTEM, user=prompt, meter=meter, model=settings.text_model,
        )
        payload = response.json()
    except LLMUnavailable:
        raise
    except Exception as exc:
        log.warning("role verification failed for %s: %s", url, exc)
        if meter is not None:
            meter.note_failure("verify_roles", exc)
        return {}

    raw_people = payload.get("people") if isinstance(payload, dict) else None
    if not isinstance(raw_people, list):
        return {}

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
