"""Vision-language extraction — the escalation path.

Used when HTML parsing comes up empty on a page that looks like a roster: client
-rendered content, layout-dependent tables, or contacts published as images.
Model output is validated field by field; anything implausible is dropped rather
than stored, because a hallucinated address is what the client would email.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db.enums import PersonCategory
from ..domain.matching import normalize_email
from ..domain.pgy import parse_class_of, parse_pgy
from ..llm.prompts import EXTRACTION_SYSTEM, extraction_user_prompt
from ..llm.provider import VisionProvider, get_provider
from ..llm.usage import UsageMeter
from ..validation.email import is_plausible
from .html_people import _extract_name, page_is_alumni_listing
from .person import ExtractedPerson

log = logging.getLogger("agentscrape.extract.vision")

_CATEGORY_MAP = {c.value: c for c in PersonCategory}


def _coerce_person(
    raw: dict[str, Any], *, page_text: str, page_is_alumni: bool = False
) -> ExtractedPerson | None:
    """Validate one model-emitted object. Returns None when it is not usable."""
    if not isinstance(raw, dict):
        return None

    name = raw.get("full_name")
    name = _extract_name(str(name)) if isinstance(name, str) and name.strip() else None

    email = raw.get("email")
    email = normalize_email(str(email)) if isinstance(email, str) else None
    if email and not is_plausible(email):
        email = None
    # An address the model produced that appears nowhere in the page text is a
    # completion, not an extraction. Drop it and keep the rest of the person.
    if email and page_text and email.lower() not in page_text.lower():
        log.info("discarding email %r: not present in page text", email)
        email = None

    category = _CATEGORY_MAP.get(
        str(raw.get("category", "")).strip().lower(), PersonCategory.UNKNOWN
    )
    position = raw.get("position")
    position = str(position).strip() if isinstance(position, str) and position.strip() else None
    # A page-level alumni listing overrides whatever the model said per person.
    if page_is_alumni:
        category = PersonCategory.ALUMNI

    pgy = raw.get("pgy")
    if isinstance(pgy, str):
        pgy = parse_pgy(pgy)
    pgy = pgy if isinstance(pgy, int) and 1 <= pgy <= 9 else None

    class_of = raw.get("class_of")
    if isinstance(class_of, str):
        class_of = parse_class_of(class_of)
    class_of = class_of if isinstance(class_of, int) and 1950 <= class_of <= 2100 else None

    specialty = raw.get("specialty")
    specialty = str(specialty).strip() if isinstance(specialty, str) and specialty.strip() else None

    person = ExtractedPerson(
        full_name=name, email=email, category=category, position=position,
        pgy=pgy, class_of=class_of, specialty_raw=specialty,
        locate_hints=[t for t in (name, email) if t],
        # Vision is inherently less certain than a labelled table.
        confidence=0.7 if (name and email) else 0.5,
        source_note="vision",
    )
    return person if person.is_usable else None


async def extract_with_vision(
    *,
    url: str,
    title: str,
    text: str,
    screenshot: bytes | None,
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
) -> list[ExtractedPerson]:
    """Ask the model to read a page. Returns [] on any failure — never raises
    into the pipeline, because one unreadable page must not fail a site."""
    provider = provider or get_provider()
    prompt = extraction_user_prompt(
        title=title, url=url, text=text, has_screenshot=screenshot is not None
    )
    try:
        response = await provider.complete(
            system=EXTRACTION_SYSTEM, user=prompt, image_bytes=screenshot, meter=meter
        )
    except Exception as exc:  # provider down, timeout, bad gateway
        log.warning("vision extraction failed for %s: %s", url, exc)
        return []

    payload = response.json()
    if payload is None:
        log.warning("vision extraction returned unparseable output for %s", url)
        return []
    if isinstance(payload, dict):
        # Models occasionally wrap the array in {"people": [...]}.
        payload = next(
            (v for v in payload.values() if isinstance(v, list)), []
        )
    if not isinstance(payload, list):
        return []

    page_is_alumni = page_is_alumni_listing(title, url)
    people = [
        p
        for p in (
            _coerce_person(item, page_text=text, page_is_alumni=page_is_alumni)
            for item in payload
        )
        if p
    ]
    log.info("vision extraction: %d people from %s", len(people), url)
    return people
