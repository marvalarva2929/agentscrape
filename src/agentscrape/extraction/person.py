"""The extracted-person shape shared by the HTML and model-reading extractors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..db.enums import PersonCategory


_POSITION_SPACE = re.compile(r"\s+")
_POSITION_PAPER_LANGUAGE = re.compile(
    r"\b(analysis|study|review|impact|effect|association|outcomes?|funding|"
    r"elective|methods?|results?|conclusion|abstract|research question)\b",
    re.IGNORECASE,
)
_POSITION_ROLE_LANGUAGE = re.compile(
    r"\b(resident|fellow|intern|student|physician|professor|attending|faculty|"
    r"director|coordinator|administrator|manager|nurse|assistant|associate|"
    r"chair|dean|chief|pgy|postdoctoral)\b",
    re.IGNORECASE,
)


def sanitize_position(value: str | None) -> str | None:
    """Keep a short printed job title, never prose accidentally called a title.

    Model output sometimes copied an article or publication heading into the
    `position` field.  A position is a compact role label; it is not a
    sentence, paper title, degree history, or a class-year field.
    """
    if not isinstance(value, str):
        return None
    position = _POSITION_SPACE.sub(" ", value).strip(" ,;:-")
    if not position or len(position) > 100:
        return None
    words = position.split()
    if len(words) > 8 or any(mark in position for mark in (".", "?", "!", "\n")):
        return None
    folded = position.casefold()
    if re.search(r"\b(class of|medical school|undergraduate|graduate school|degree)\b", folded):
        return None
    # A long phrase without a recognised employment/training title is almost
    # always page prose.  Paper vocabulary makes it unsafe at any length.
    if _POSITION_PAPER_LANGUAGE.search(position):
        return None
    if len(words) > 4 and not _POSITION_ROLE_LANGUAGE.search(position):
        return None
    return position


@dataclass
class ExtractedPerson:
    full_name: str | None = None
    email: str | None = None
    # Coarse bucket for filtering.
    category: PersonCategory = PersonCategory.UNKNOWN
    # The title exactly as the page printed it.
    position: str | None = None
    pgy: int | None = None
    class_of: int | None = None
    specialty_raw: str | None = None
    confidence: float = 0.5
    source_note: str | None = None

    @property
    def is_usable(self) -> bool:
        """A person needs a name or an email; anything else is a parsing artifact."""
        return bool((self.full_name and self.full_name.strip()) or self.email)

    def to_fields(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "email": self.email,
            "category": str(self.category),
            "position": self.position,
            "pgy": self.pgy,
            "class_of": self.class_of,
            "specialty_raw": self.specialty_raw,
        }
