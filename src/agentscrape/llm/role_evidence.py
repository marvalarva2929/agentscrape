"""Regex evidence patterns for grounding a person's category in page text.

Shared by crawl-time reading (`reader.py`) and role verification (`verify.py`)
so "what counts as resident evidence" cannot drift between the two: a category
is only trustworthy when the page actually names it, not from page context or
proximity to a residency program.
"""

from __future__ import annotations

import re

ROLE_EVIDENCE: dict[str, re.Pattern[str]] = {
    "resident": re.compile(
        r"\b(residents?|interns?|house[- ]staff|pgy[- ]?\d|ca[- ]?[123]|chief residents?)\b",
        re.IGNORECASE,
    ),
    "fellow": re.compile(r"\bfellows?(hip)?\b", re.IGNORECASE),
    "faculty": re.compile(r"\b(faculty|attending|professor|program director)\b", re.IGNORECASE),
    "staff": re.compile(r"\b(staff|coordinator|administrator)\b", re.IGNORECASE),
    "student": re.compile(r"\b(students?|ms[1-4])\b", re.IGNORECASE),
    "alumni": re.compile(r"\b(alumni|alumnus|alumna|graduates?|former|past)\b", re.IGNORECASE),
}


def is_grounded(role: str, evidence: str) -> bool:
    """Whether `evidence` actually supports `role` under its regex."""
    pattern = ROLE_EVIDENCE.get(role)
    return bool(pattern and evidence and pattern.search(evidence))


def is_alumni_flagged(evidence: str) -> bool:
    """A former resident/fellow is alumni, never current, however it reads."""
    return bool(evidence and ROLE_EVIDENCE["alumni"].search(evidence))
