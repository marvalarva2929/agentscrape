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
from dataclasses import dataclass
from datetime import UTC, date, datetime

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


def current_pgy(
    pgy_at_capture: int | None, capture_date: date | None, *, today: date | None = None
) -> int | None:
    """Roll a captured PGY forward to today across July 1 boundaries.

    Returns None once the person would have finished any plausible program
    (>9), rather than reporting a nonsense level for stale data.
    """
    if pgy_at_capture is None or capture_date is None:
        return None
    today = today or datetime.now(UTC).date()
    advanced = pgy_at_capture + (academic_year(today) - academic_year(capture_date))
    if advanced < 1 or advanced > 9:
        return None
    return advanced


def class_of_from_pgy(
    pgy_at_capture: int, capture_date: date, final_pgy: int
) -> int | None:
    """Graduation calendar year implied by a PGY level seen on a given date.

    `final_pgy` is the PGY level held in the last year of the programme (see
    SPECIALTY_FINAL_PGY). A PGY-k in academic year AY has (final_pgy - k) years
    left, and academic year AY ends in calendar year AY+1.
    """
    if pgy_at_capture < 1 or final_pgy < pgy_at_capture:
        return None
    return academic_year(capture_date) + 1 + (final_pgy - pgy_at_capture)


def pgy_from_class_of(
    class_of: int, capture_date: date, final_pgy: int
) -> int | None:
    """Inverse of `class_of_from_pgy`: PGY level at capture time."""
    level = final_pgy - class_of + academic_year(capture_date) + 1
    if 1 <= level <= final_pgy:
        return level
    return None


@dataclass(frozen=True)
class YearFields:
    pgy_at_capture: int | None
    pgy_source: str | None  # "extracted" | "derived"
    class_of: int | None
    class_of_source: str | None


def resolve_year_fields(
    *,
    pgy_at_capture: int | None,
    class_of: int | None,
    capture_date: date,
    program_years: int | None,
) -> YearFields:
    """Fill in whichever of PGY / class-of is missing, tagging derived values.

    Back-fill needs the program length, so it only happens once the specialty is
    known and mapped. Derived values are marked so the frontend and any downstream
    consumer can tell an inference from something the page actually said.
    """
    pgy_source = "extracted" if pgy_at_capture is not None else None
    class_source = "extracted" if class_of is not None else None

    if program_years:
        if pgy_at_capture is None and class_of is not None:
            derived = pgy_from_class_of(class_of, capture_date, program_years)
            if derived is not None:
                pgy_at_capture, pgy_source = derived, "derived"
        elif class_of is None and pgy_at_capture is not None:
            derived = class_of_from_pgy(pgy_at_capture, capture_date, program_years)
            if derived is not None:
                class_of, class_source = derived, "derived"

    return YearFields(pgy_at_capture, pgy_source, class_of, class_source)
