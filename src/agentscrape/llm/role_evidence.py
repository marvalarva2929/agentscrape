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
# A page's topic is not a person's GME status.  These are governance,
# participation, historical, recruitment, and non-clinical-training contexts
# commonly co-located with residency/fellowship material.  A person named in
# one needs an explicit current GME title or level, not just the word
# "resident" or "fellow" in the section name.
_NON_ROSTER_CONTEXT = re.compile(
    r"\b(committee|advis(?:ory|er)|board|council|task\s*force|working\s*group|"
    r"steering\s*(?:group|committee)|subcommittee|assembly|caucus|"
    r"member(?:ship)?|representative|liaison|ambassador|leadership|"
    r"faculty|staff|coordinator|administrator|mentor|preceptor|attending|"
    r"alumni|graduate|former|past|histor(?:y|ical)|archive|class\s+of\s+20\d{2}|"
    r"award|nomination|speaker|presenter|author|publication|news|article|"
    r"applicant|candidate|interviewee|matched|incoming|prospective|visiting|"
    r"medical\s+student|postdoc(?:toral)?|research\s+fellows?|teaching\s+fellows?)\b",
    re.IGNORECASE,
)
_GOVERNANCE_OR_NON_GME_CONTEXT = re.compile(
    r"\b(committee|advis(?:ory|er)|board|council|task\s*force|working\s*group|"
    r"steering\s*(?:group|committee)|subcommittee|assembly|caucus|"
    r"member(?:ship)?|representative|liaison|ambassador|leadership|"
    r"faculty|staff|coordinator|administrator|mentor|preceptor|attending|"
    r"postdoc(?:toral)?|research\s+fellows?|teaching\s+fellows?|visiting\s+fellows?)\b",
    re.IGNORECASE,
)
_DIRECT_TRAINEE_EVIDENCE = re.compile(
    r"\b(pgy[- ]?\d|ca[- ]?[123]|resident physician|chief resident|"
    r"current resident|current fellow|clinical fellow)\b",
    re.IGNORECASE,
)


def has_direct_current_trainee_evidence(evidence: str) -> bool:
    """Whether text explicitly identifies a person as a current GME trainee."""
    return bool(evidence and _DIRECT_TRAINEE_EVIDENCE.search(evidence))


def is_non_roster_context(evidence: str) -> bool:
    """Whether text describes a group/context rather than a current GME roster."""
    return bool(evidence and _NON_ROSTER_CONTEXT.search(evidence))


def is_governance_or_non_gme_context(evidence: str) -> bool:
    """Whether text positively rules out a roster absent a direct GME title."""
    return bool(evidence and _GOVERNANCE_OR_NON_GME_CONTEXT.search(evidence))


def is_grounded(role: str, evidence: str) -> bool:
    """Whether `evidence` actually supports `role` under its regex."""
    pattern = ROLE_EVIDENCE.get(role)
    if not (pattern and evidence and pattern.search(evidence)):
        return False
    # "Resident Advisory Committee", "Fellowship Board", or "research
    # fellow" identify a group, a historical/recruiting status, or a
    # non-GME appointment, not a current GME role.  A committee member can
    # still be a trainee, but that requires a direct current title/level.
    if role in ("resident", "fellow") and is_non_roster_context(evidence):
        return has_direct_current_trainee_evidence(evidence)
    return True


def is_alumni_flagged(evidence: str) -> bool:
    """A former resident/fellow is alumni, never current, however it reads."""
    return bool(evidence and ROLE_EVIDENCE["alumni"].search(evidence))
