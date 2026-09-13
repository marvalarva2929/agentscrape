"""Post-secondary validation gate.

Scope constraint: the system must not collect from K-12 institutions. This is an
explicit validation step with a stated reason, never a silent filter — a rejected
site produces a SiteRun with K12_INSTITUTION_REJECTED and surfaces in the
/runs/validate preview so the client sees it before spending compute.

Cheap signals decide the clear cases. Only genuine ambiguity reaches the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..db.enums import ValidationStatus
from ..urls import host_of

# Domain shapes that are conclusively K-12.
_K12_DOMAIN_PATTERNS = (
    re.compile(r"(^|\.)k12\.[a-z]{2}\.us$", re.IGNORECASE),
    re.compile(r"(^|\.)[a-z]+isd\.(org|net|com|edu)$", re.IGNORECASE),   # independent school district
    re.compile(r"(^|\.)[a-z]+usd\d*\.(org|net|com|edu)$", re.IGNORECASE),  # unified school district
    re.compile(r"(^|\.)(schools?|schooldistrict|pusd|cusd|dusd)\.[a-z.]+$", re.IGNORECASE),
)
# Tokens in the hostname that strongly imply primary/secondary education.
_K12_HOST_TOKENS = (
    "elementaryschool", "elementary", "middleschool", "highschool", "highschools",
    "primaryschool", "gradeschool", "prepschool", "k12", "schooldistrict",
    "publicschools", "privateschool", "charterschool", "academyschools",
)
# Content phrases. Weighted because "academy" and "college" are ambiguous alone.
_K12_CONTENT_STRONG = (
    "school district", "elementary school", "middle school", "junior high",
    "high school students", "grades k-12", "grades k through 12", "kindergarten",
    "pre-k", "prek", "parent portal", "powerschool", "board of education",
    "superintendent of schools", "enroll your child", "our students and families",
)
_POST_SECONDARY_STRONG = (
    "residency program", "residency", "fellowship program", "graduate medical education",
    "school of medicine", "college of medicine", "medical school", "medical center",
    "teaching hospital", "undergraduate admissions", "graduate school",
    "faculty of medicine", "academic medical center", "postgraduate", "phd program",
    "school of nursing", "school of public health", "accreditation council for graduate",
    "acgme", "department of surgery", "department of medicine", "attending physician",
    "house staff", "housestaff", "clerkship", "match day",
)
# .edu is US-restricted to accredited post-secondary institutions, with the
# notable exception of legacy k12.*.us delegations handled above.
_POST_SECONDARY_TLD_HINTS = (".edu", ".ac.uk", ".edu.au", ".ac.jp", ".edu.sg")


@dataclass(frozen=True)
class InstitutionVerdict:
    status: ValidationStatus
    reason: str
    institution_type: str | None = None
    needs_model_review: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == ValidationStatus.POST_SECONDARY


def classify_domain(url_or_host: str) -> InstitutionVerdict | None:
    """Decide from the hostname alone. Returns None when the host is inconclusive."""
    host = host_of(url_or_host) or url_or_host.lower().strip("/")
    if not host:
        return InstitutionVerdict(
            ValidationStatus.UNKNOWN, "No hostname could be parsed from the input."
        )

    for pattern in _K12_DOMAIN_PATTERNS:
        if pattern.search(host):
            return InstitutionVerdict(
                ValidationStatus.K12_REJECTED,
                f"Hostname '{host}' matches a K-12 school-district domain pattern "
                f"({pattern.pattern}). Out of scope: post-secondary only.",
                institution_type="k12",
            )

    flattened = host.replace("-", "").replace(".", "")
    for token in _K12_HOST_TOKENS:
        if token in flattened:
            return InstitutionVerdict(
                ValidationStatus.K12_REJECTED,
                f"Hostname '{host}' contains the K-12 indicator '{token}'. "
                f"Out of scope: post-secondary only.",
                institution_type="k12",
            )

    if any(host.endswith(suffix) for suffix in _POST_SECONDARY_TLD_HINTS):
        return InstitutionVerdict(
            ValidationStatus.POST_SECONDARY,
            f"Hostname '{host}' uses a TLD restricted to accredited post-secondary "
            f"institutions.",
            institution_type="university",
        )
    return None


def classify_content(host: str, page_text: str, page_title: str = "") -> InstitutionVerdict:
    """Decide from homepage text once the hostname was inconclusive."""
    haystack = f"{page_title}\n{page_text}".lower()[:200_000]
    k12_hits = [p for p in _K12_CONTENT_STRONG if p in haystack]
    post_hits = [p for p in _POST_SECONDARY_STRONG if p in haystack]

    if k12_hits and not post_hits:
        return InstitutionVerdict(
            ValidationStatus.K12_REJECTED,
            f"Homepage content indicates a primary/secondary school "
            f"(matched: {', '.join(k12_hits[:4])}). Out of scope: post-secondary only.",
            institution_type="k12",
        )
    if post_hits and not k12_hits:
        return InstitutionVerdict(
            ValidationStatus.POST_SECONDARY,
            f"Homepage content indicates a post-secondary or academic medical "
            f"institution (matched: {', '.join(post_hits[:4])}).",
            institution_type="academic_medical_center"
            if any("medic" in p or "residency" in p or "hospital" in p for p in post_hits)
            else "university",
        )
    if post_hits and k12_hits:
        # Universities host lab schools; the post-secondary presence wins but is
        # worth recording, and the model gets a look.
        if len(post_hits) > len(k12_hits):
            return InstitutionVerdict(
                ValidationStatus.POST_SECONDARY,
                f"Mixed signals; post-secondary evidence dominates "
                f"({len(post_hits)} vs {len(k12_hits)} indicators).",
                institution_type="university",
                needs_model_review=True,
            )
        return InstitutionVerdict(
            ValidationStatus.UNKNOWN,
            f"Mixed signals; K-12 evidence dominates "
            f"({len(k12_hits)} vs {len(post_hits)} indicators). Needs review.",
            needs_model_review=True,
        )
    return InstitutionVerdict(
        ValidationStatus.UNKNOWN,
        f"No conclusive institution-type signal found for '{host}'.",
        needs_model_review=True,
    )
