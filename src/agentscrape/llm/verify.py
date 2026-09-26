"""Role verification: re-reads one already-scraped page and chooses one
grounded category for each person the crawl found there.

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
from ..extraction.person import sanitize_position
from .prompts import VERIFY_ROLES_SYSTEM, verify_roles_user_prompt
from .provider import VisionProvider, get_provider
from .reader import fold, name_in_text
from .role_evidence import is_alumni_flagged, is_grounded
from .usage import LLMUnavailable, UsageMeter

log = logging.getLogger("agentscrape.llm.verify")

_ROLES = frozenset(c.value for c in PersonCategory)
# A large roster used to place every person and every local excerpt in one
# request.  That routinely exceeded provider/gateway limits and made an entire
# page look like a technical verification failure.  Small batches preserve
# grounded evidence while keeping each request predictable.
_VERIFY_BATCH_SIZE = 12


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


@dataclass(frozen=True)
class RoleDecision:
    role: str
    evidence: str
    position: str | None = None


def _packets(text: str, people: list[RoleCheckInput]) -> dict[str, str]:
    """Give the model each person's local heading and entry, never a truncated page."""
    lines = text.splitlines()
    folded_lines = [fold(line) for line in lines]
    packets: dict[str, str] = {}
    for person in people:
        name_words = [word for word in fold(person.full_name).split() if len(word) > 1]
        hits = [i for i, line in enumerate(folded_lines) if name_words and name_words[0] in line and name_words[-1] in line]
        if not hits:
            continue
        i = hits[0]
        heading = ""
        for prior in range(i, max(-1, i - 25), -1):
            if lines[prior].lstrip().startswith("#"):
                heading = lines[prior].strip()
                break
        excerpt = "\n".join(lines[max(0, i - 3): min(len(lines), i + 7)])
        packets[person.record_id] = f"Section: {heading or '(none)'}\nExcerpt:\n{excerpt}"[:1_500]
    return packets


async def verify_page_roles(
    *,
    url: str,
    title: str,
    text: str,
    people: list[RoleCheckInput],
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
    attempts: int = 2,
) -> dict[str, RoleDecision] | None:
    """One role per record id, grounded against the page text.

    A record missing from the model's response, or whose response could not
    be grounded, is left out of the returned dict entirely - the caller keeps
    that record's existing category rather than treating "not returned" as
    "no roles". `attempts` is how many requests one batch
    gets before unparseable output is given up on. Returns `None`, not `{}`, when the call itself never produced
    a usable response (a hard provider/model failure, or unparseable output
    even after a retry) - the caller must be able to tell that apart from "the
    model answered but grounded nobody", which returns `{}`/a partial dict.
    """
    if not people:
        return {}
    provider = provider or get_provider()
    by_name: dict[str, list[RoleCheckInput]] = {}
    for person in people:
        by_name.setdefault(person.full_name, []).append(person)

    packets = _packets(text, people)
    out: dict[str, RoleDecision] = {}
    successful_batches = 0
    for start in range(0, len(people), _VERIFY_BATCH_SIZE):
        batch = people[start : start + _VERIFY_BATCH_SIZE]
        batch_names = {person.full_name for person in batch}
        prompt = verify_roles_user_prompt(
            url=url, title=title, text=text,
            people=[
                {"name": p.full_name, "category": p.category, "position": p.position,
                 "packet": packets.get(p.record_id, "not found in readable source")}
                for p in batch
            ],
        )
        payload = None
        failure_recorded = False
        for attempt in range(max(1, attempts)):
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
                log.warning("role verification failed for %s batch %d: %s", url, start // _VERIFY_BATCH_SIZE + 1, exc)
                if meter is not None:
                    meter.note_failure("verify_roles", exc)
                failure_recorded = True
                payload = None
                break
            if _is_answer(payload):
                break
            log.warning("role verification for %s batch %d returned unparseable output (attempt %d)", url, start // _VERIFY_BATCH_SIZE + 1, attempt + 1)
            payload = None
        if payload is None:
            if meter is not None and not failure_recorded:
                meter.note_failure("verify_roles", "unparseable output")
            continue
        successful_batches += 1
        for entry in payload.get("people", []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or name not in batch_names:
                continue
            role = entry.get("role")
            if not isinstance(role, str):
                continue
            role = role.strip().lower()
            if role not in _ROLES:
                continue
            evidence = entry.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                continue
            evidence = evidence.strip()[:300]
            position = sanitize_position(entry.get("position"))
            position_evidence = entry.get("position_evidence")
            if not isinstance(position_evidence, str) or not position_evidence.strip():
                position = None
            else:
                position_evidence = position_evidence.strip()[:300]
            for candidate in by_name[name]:
                packet = packets.get(candidate.record_id, "")
                if not name_in_text(candidate.full_name, fold(packet)) or evidence.casefold() not in packet.casefold():
                    continue
                if position is not None and (
                    position.casefold() not in packet.casefold()
                    or position_evidence.casefold() not in packet.casefold()
                ):
                    position = None
                if role != "unknown" and not is_grounded(role, evidence):
                    continue
                if role == "resident" and is_alumni_flagged(evidence):
                    continue
                out[candidate.record_id] = RoleDecision(role, evidence, position)
    # A partial response is still valuable.  Only report a technical page
    # failure when every bounded request failed.
    return out if successful_batches else None
