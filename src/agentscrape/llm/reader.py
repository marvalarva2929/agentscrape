"""The model reads every page: who is on it, what kind of page it is, what is hidden.

This replaces keyword escalation (`page_looks_thin`) and regex classification as
the primary path. The regex extractor still runs, but only to fill in addresses
and on-screen locations, and as the fallback when the model call fails.

Deterministic guardrails stay on the model's output: an email must appear in the
page text, and so must the person's first and last name. A dropped value is
recoverable on the next run; an invented person or address is what the client
would actually send mail to.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..db.enums import PersonCategory
from ..domain.matching import normalize_email, normalize_name
from ..domain.pgy import parse_class_of, parse_pgy
from ..extraction.person import ExtractedPerson
from ..extraction.text import chunk_text
from ..validation.email import is_plausible
from .prompts import READER_SYSTEM, reader_user_prompt
from .provider import ModelTimeout, VisionProvider, get_provider
from .usage import LLMUnavailable, UsageMeter

log = logging.getLogger("agentscrape.llm.reader")

_CATEGORY = {c.value: c for c in PersonCategory}
_TITLE_PREFIX = re.compile(r"^(dr|doctor|prof|professor|mr|mrs|ms|mx)\.?\s+", re.IGNORECASE)
_DEGREES = re.compile(
    r"(,\s*|\s+)(M\.?D|D\.?O|Ph\.?D|M\.?B\.?B\.?S|M\.?P\.?H|M\.?S|M\.?A|M\.?B\.?A|"
    r"M\.?Sc|B\.?S|B\.?A|R\.?N|D\.?D\.?S|D\.?M\.?D|Pharm\.?D|MSCI|MSc|MHA|MEd|MS-HPEd|"
    r"FAAP|FACP|FACS|FACEP)\.?(?=,|\s|$)",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")


@dataclass
class PageReading:
    ok: bool = False
    page_type: str = "unknown"
    program: str | None = None
    is_current_trainee_roster: bool = False
    expected_people_count: int = 0
    hidden_content: str | None = None
    needs_render: bool = False
    render_reason: str | None = None
    people: list[ExtractedPerson] = field(default_factory=list)

    @property
    def looks_incomplete(self) -> bool:
        """The page lists more people than we managed to read off it."""
        found = len(self.people)
        if self.is_current_trainee_roster and found == 0:
            return True
        return self.expected_people_count > max(found * 1.3, found + 2)


def fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", text or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return _WS.sub(" ", value.lower())


def clean_model_name(raw: str | None) -> str | None:
    """Tidy a model-reported name. Deliberately lenient: the in-text check is
    what keeps invented people out, not a name-shape regex (which dropped every
    all-caps roster)."""
    if not isinstance(raw, str):
        return None
    name = _WS.sub(" ", raw).strip(" ,.;:-")
    name = _TITLE_PREFIX.sub("", name)
    previous = None
    while previous != name:
        previous = name
        name = _DEGREES.sub("", name).strip(" ,.;:-")
    if not name or "@" in name or any(ch.isdigit() for ch in name):
        return None
    words = name.split()
    if not 2 <= len(words) <= 7:
        # Mononyms and whole sentences are not directory entries.
        return None
    if name.isupper():
        name = " ".join(w.capitalize() if len(w) > 2 else w for w in words)
    return name


def name_in_text(name: str, folded_text: str) -> bool:
    """First and last name both appear on the page, accent- and case-folded."""
    words = [w.strip(".,'’()\"") for w in fold(name).split()]
    words = [w for w in words if len(w) > 1]
    if len(words) < 2:
        return False
    return words[0] in folded_text and words[-1] in folded_text


def coerce_person(
    raw: Any, *, folded_text: str, program: str | None, source: str
) -> ExtractedPerson | None:
    if not isinstance(raw, dict):
        return None
    name = clean_model_name(raw.get("full_name"))
    if name and not name_in_text(name, folded_text):
        log.info("discarding %r: name not present in page text", name)
        name = None

    email = raw.get("email")
    email = normalize_email(str(email)) if isinstance(email, str) else None
    if email and (not is_plausible(email) or email not in folded_text):
        email = None
    if not name and not email:
        return None

    category = _CATEGORY.get(str(raw.get("category", "")).strip().lower(), PersonCategory.UNKNOWN)
    position = raw.get("position")
    position = _WS.sub(" ", position).strip()[:200] if isinstance(position, str) and position.strip() else None

    pgy = raw.get("pgy")
    if isinstance(pgy, str):
        pgy = int(pgy) if pgy.strip().isdigit() else parse_pgy(pgy)
    pgy = pgy if isinstance(pgy, int) and not isinstance(pgy, bool) and 1 <= pgy <= 9 else None
    class_of = raw.get("class_of")
    if isinstance(class_of, str):
        class_of = int(class_of) if class_of.strip().isdigit() else parse_class_of(class_of)
    class_of = (
        class_of
        if isinstance(class_of, int) and not isinstance(class_of, bool) and 1950 <= class_of <= 2100
        else None
    )
    specialty = raw.get("specialty")
    specialty = specialty.strip()[:200] if isinstance(specialty, str) and specialty.strip() else program

    return ExtractedPerson(
        full_name=name, email=email, category=category, position=position,
        pgy=pgy, class_of=class_of, specialty_raw=specialty,
        locate_hints=[t for t in (name, email) if t],
        confidence=0.85 if (name and email) else 0.7,
        source_note=source,
    )


def _person_key(person: ExtractedPerson) -> str:
    if person.email:
        return f"e:{person.email}"
    folded = normalize_name(person.full_name) or fold(person.full_name or "")
    words = folded.split()
    return f"n:{words[0]} {words[-1]}" if len(words) >= 2 else f"n:{folded}"


def merge_people(*groups: list[ExtractedPerson]) -> list[ExtractedPerson]:
    """Union by email, then first+last name. Earlier groups win on conflicts,
    but a later sighting fills in fields the earlier one lacked."""
    merged: dict[str, ExtractedPerson] = {}
    by_name: dict[str, str] = {}
    for group in groups:
        for person in group:
            name_key = None
            if person.full_name:
                words = (normalize_name(person.full_name) or fold(person.full_name)).split()
                if len(words) >= 2:
                    name_key = f"{words[0]} {words[-1]}"
            key = _person_key(person)
            existing_key = key if key in merged else by_name.get(name_key or "")
            if existing_key is None:
                merged[key] = person
                if name_key:
                    by_name.setdefault(name_key, key)
                continue
            current = merged[existing_key]
            for attr in ("full_name", "email", "position", "pgy", "class_of", "specialty_raw"):
                if getattr(current, attr) in (None, "") and getattr(person, attr) not in (None, ""):
                    setattr(current, attr, getattr(person, attr))
            if current.category == PersonCategory.UNKNOWN and person.category != PersonCategory.UNKNOWN:
                current.category = person.category
            current.locate_hints = list(dict.fromkeys([*current.locate_hints, *person.locate_hints]))
            if name_key:
                by_name.setdefault(name_key, existing_key)
    return list(merged.values())


# How many times a timed-out chunk is halved before the read is given up.
MAX_SPLITS = 2
_MIN_SPLIT_CHARS = 2_000


async def _read_chunk(
    *, provider: VisionProvider, url: str, title: str, text: str, part: int, parts: int,
    screenshot: bytes | None, meter: UsageMeter | None, splits_left: int = MAX_SPLITS,
) -> list[dict]:
    """Payloads for one chunk: one normally, several when a timeout split it."""
    prompt = reader_user_prompt(url=url, title=title, text=text, part=part, parts=parts)
    model = settings.llm_model if screenshot else settings.text_model
    for attempt in range(2):
        try:
            response = await provider.complete(
                system=READER_SYSTEM, user=prompt, image_bytes=screenshot,
                meter=meter, model=model,
            )
        except LLMUnavailable:
            raise
        except ModelTimeout as exc:
            if splits_left > 0 and len(text) > _MIN_SPLIT_CHARS:
                # A gateway timeout means the read was too long to finish, so
                # read half as much at a time: less text in, fewer people out.
                halves = chunk_text(text, len(text) // 2 + 500, overlap=500)
                log.info(
                    "reading %s timed out; splitting %d chars into %d parts",
                    url, len(text), len(halves),
                )
                results = await asyncio.gather(*(
                    _read_chunk(
                        provider=provider, url=url, title=title, text=half, part=part,
                        parts=parts, screenshot=screenshot if i == 0 else None,
                        meter=meter, splits_left=splits_left - 1,
                    )
                    for i, half in enumerate(halves)
                ))
                return [payload for group in results for payload in group]
            log.warning("page reading timed out for %s (part %d/%d): %s", url, part, parts, exc)
            if meter is not None:
                meter.note_failure("read_page", exc)
            return []
        except Exception as exc:
            log.warning("page reading failed for %s (part %d/%d): %s", url, part, parts, exc)
            if meter is not None:
                meter.note_failure("read_page", exc)
            return []
        payload = response.json()
        if isinstance(payload, list):
            payload = {"people": payload}
        if isinstance(payload, dict):
            return [payload]
        # Unparseable usually means the JSON was cut off; one retry, then give up.
        log.warning("page reading for %s returned unparseable output (attempt %d)", url, attempt + 1)
    if meter is not None:
        meter.note_failure("read_page", "unparseable output")
    return []


async def read_page(
    *,
    url: str,
    title: str,
    text: str,
    screenshot: bytes | None = None,
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
) -> PageReading:
    """Have the model read a page. `ok` is False when every call failed, so the
    caller can fall back to the regex extractor."""
    provider = provider or get_provider()
    chunks = chunk_text(text or "", settings.llm_page_chunk_chars) or [""]
    payloads = await asyncio.gather(*(
        _read_chunk(
            provider=provider, url=url, title=title, text=chunk, part=i + 1,
            parts=len(chunks), screenshot=screenshot if i == 0 else None, meter=meter,
        )
        for i, chunk in enumerate(chunks)
    ))
    good = [p for group in payloads for p in group]
    if not good:
        return PageReading(ok=False)

    reading = PageReading(ok=True)
    folded = fold(text)
    groups: list[list[ExtractedPerson]] = []
    for payload in good:
        page_type = str(payload.get("page_type") or "").strip().lower()[:40]
        if page_type and reading.page_type in ("unknown", "other"):
            reading.page_type = page_type
        program = payload.get("program")
        if isinstance(program, str) and program.strip() and not reading.program:
            reading.program = program.strip()[:200]
        reading.is_current_trainee_roster |= payload.get("is_current_trainee_roster") is True
        reading.needs_render |= payload.get("needs_render") is True
        count = payload.get("expected_people_count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            reading.expected_people_count += count
        for key in ("hidden_content", "render_reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip() and not getattr(reading, key):
                setattr(reading, key, value.strip()[:300])
        raw_people = payload.get("people") if isinstance(payload.get("people"), list) else []
        groups.append([
            p for p in (
                coerce_person(raw, folded_text=folded, program=reading.program, source="llm")
                for raw in raw_people
            ) if p
        ])
    reading.people = merge_people(*groups)
    log.info(
        "read %s: %s, %d people (expected %d)%s%s",
        url, reading.page_type, len(reading.people), reading.expected_people_count,
        " [trainee roster]" if reading.is_current_trainee_roster else "",
        f" hidden: {reading.hidden_content}" if reading.hidden_content else "",
    )
    return reading


def combine_with_regex(
    model_people: list[ExtractedPerson], regex_people: list[ExtractedPerson], folded_text: str
) -> list[ExtractedPerson]:
    """Model output is primary. The regex extractor fills in addresses and
    positions for people the model found, and adds a person the model did not
    return only when an address on the page anchors them."""
    model_ids = {id(p) for p in model_people}
    merged = merge_people(model_people, regex_people)
    out: list[ExtractedPerson] = []
    for person in merged:
        if id(person) in model_ids:
            out.append(person)
        elif person.email and person.email in folded_text:
            # The address is real, but a name and role the model did not
            # confirm are regex guesses ("Infant Breast Feeding", "Cambridge
            # St"); keep only what the page proves.
            person.full_name = None
            person.position = None
            person.category = PersonCategory.UNKNOWN
            person.locate_hints = [person.email]
            out.append(person)
    return out
