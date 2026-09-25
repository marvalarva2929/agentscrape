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

# These headings often live on residency/fellowship sites and frequently use
# the word "resident" or "fellow" in the name of the group.  Membership is
# not proof that a person currently holds the trainee role.
_COMMITTEE_CONTEXT = re.compile(
    r"\b(committee|advisory|board|council|member(?:ship)?|leadership)\b",
    re.IGNORECASE,
)
_DIRECT_TRAINEE_EVIDENCE = re.compile(
    r"\b(pgy[- ]?\d|ca[- ]?[123]|resident physician|chief resident|"
    r"(?:clinical|research) fellow)\b",
    re.IGNORECASE,
)


def is_grounded(role: str, evidence: str) -> bool:
    """Whether `evidence` actually supports `role` under its regex."""
    pattern = ROLE_EVIDENCE.get(role)
    if not (pattern and evidence and pattern.search(evidence)):
        return False
    # "Resident Advisory Committee" and "Fellowship Board" identify a
    # group, not its members' current role.  Preserve a direct title/PGY on
    # the same evidence line: committee membership and trainee status can
    # both be true, but the latter must be explicit.
    if role in ("resident", "fellow") and _COMMITTEE_CONTEXT.search(evidence):
        return bool(_DIRECT_TRAINEE_EVIDENCE.search(evidence))
    return True


def is_alumni_flagged(evidence: str) -> bool:
    """A former resident/fellow is alumni, never current, however it reads."""
    return bool(evidence and ROLE_EVIDENCE["alumni"].search(evidence))
