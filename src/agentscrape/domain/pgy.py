"""PGY and Class-of handling.

PGY is *time-relative*: a PGY-2 in 2026 is a PGY-3 in 2027. Class-of is absolute.
So we store what the page said (`pgy_at_capture`) together with the date we saw it,
and derive `pgy_current` on read. Two consequences that matter:

  * A saved "PGY-3" filter keeps meaning "PGY-3 today" as runs age.
  * The July rollover is *not* a data change, so reconciliation must compare
    `pgy_at_capture`, never the derived value.

Residency years run July 1 - June 30. Academic year N means July N .. June N+1.
"""

from __future__ import annotations

import re
from datetime import date

ACADEMIC_YEAR_START_MONTH = 7  # July 1

_PGY_PATTERNS = (
    re.compile(r"\bpgy\s*[-–—]?\s*(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bpgy(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bpost[-\s]?graduate\s+year\s+(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bpost[-\s]?graduate\s+year\s+(one|two|three|four|five|six|seven)\b", re.IGNORECASE),
    # "R2" / "R-2" are common on residency rosters; "G2" appears on some.
    re.compile(r"\b[rg]\s*[-–—]?\s*([1-7])\b", re.IGNORECASE),
)
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7,
}
_CLASS_OF_PATTERNS = (
    re.compile(r"\bclass\s+of\s+(?:'|’)?(\d{2}|\d{4})\b", re.IGNORECASE),
    re.compile(r"\bc/?o\s+(?:'|’)?(\d{2}|\d{4})\b", re.IGNORECASE),
    re.compile(r"\bgraduat(?:es|ing|ion)\s+(?:in\s+)?(\d{4})\b", re.IGNORECASE),
    re.compile(r"\bexpected\s+graduation[:\s]+(\d{4})\b", re.IGNORECASE),
)


def academic_year(on: date) -> int:
    """Academic year containing `on`. July 2026 and March 2027 are both AY2026."""
    return on.year if on.month >= ACADEMIC_YEAR_START_MONTH else on.year - 1


def parse_pgy(text: str | None) -> int | None:
    """Extract a PGY level from free text. Returns None when absent or implausible."""
    if not text:
        return None
    for pattern in _PGY_PATTERNS:
        match = pattern.search(text)
        if match:
            token = match.group(1).lower()
            value = _WORD_NUMBERS.get(token) if not token.isdigit() else int(token)
            if value is not None and 1 <= value <= 9:
                return value
    return None


def parse_class_of(text: str | None) -> int | None:
    """Extract a graduation year. Two-digit years resolve to 2000-2099."""
    if not text:
        return None
    for pattern in _CLASS_OF_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1)
            year = int(raw)
            if len(raw) == 2:
                year += 2000
            if 1950 <= year <= 2100:
                return year
    return None


# Deliberately no inference lives here any more.
#
# We previously rolled a captured PGY forward each 1 July, and back-filled
# class-of from PGY (and vice versa) using typical programme lengths. Product
# decided that anything the page does not state should stay blank: institutions
# update their sites whenever they like, so a value we compute is a value we can
# be wrong about. What we store and display is exactly what was published, with
# the capture date beside it.
