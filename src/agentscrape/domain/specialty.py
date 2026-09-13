"""Controlled ACGME specialty vocabulary and normalizer.

Specialty is a *page* property, not a site property: one teaching hospital publishes
residents across many programs. We infer it from URL path + page title + breadcrumbs
and collapse the result onto a fixed vocabulary so that "Dept. of Medicine",
"Internal Medicine Residency" and "IM" become one filter value.

The raw string is always stored alongside the normalized one, so a normalization
mistake is recoverable without a re-scrape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The canonical specialty vocabulary. Free-text specialties from pages are
# collapsed onto these so filtering works.
CANONICAL_SPECIALTIES_SOURCE: tuple[str, ...] = (
    "Anesthesiology", "Cardiology", "Child Neurology", "Dermatology",
    "Emergency Medicine", "Family Medicine", "Gastroenterology",
    "General Surgery", "Hematology and Oncology", "Infectious Disease",
    "Internal Medicine", "Interventional Radiology", "Medical Genetics",
    "Nephrology", "Neurological Surgery", "Neurology", "Nuclear Medicine",
    "Obstetrics and Gynecology", "Ophthalmology",
    "Oral and Maxillofacial Surgery", "Orthopaedic Surgery", "Otolaryngology",
    "Pathology", "Pediatrics", "Physical Medicine and Rehabilitation",
    "Plastic Surgery", "Preventive Medicine", "Psychiatry",
    "Pulmonary and Critical Care", "Radiation Oncology", "Radiology",
    "Rheumatology", "Thoracic Surgery", "Urology", "Vascular Surgery",
)

CANONICAL_SPECIALTIES: tuple[str, ...] = tuple(sorted(CANONICAL_SPECIALTIES_SOURCE))

# Alias -> canonical. Keys are matched after _squash() (lowercased, alnum + single spaces).
_ALIASES: dict[str, str] = {
    "im": "Internal Medicine",
    "internal med": "Internal Medicine",
    "medicine": "Internal Medicine",
    "general internal medicine": "Internal Medicine",
    "gim": "Internal Medicine",
    "med peds": "Internal Medicine",
    "fm": "Family Medicine",
    "family med": "Family Medicine",
    "family practice": "Family Medicine",
    "family and community medicine": "Family Medicine",
    "em": "Emergency Medicine",
    "emergency med": "Emergency Medicine",
    "peds": "Pediatrics",
    "pediatric": "Pediatrics",
    "ob gyn": "Obstetrics and Gynecology",
    "obgyn": "Obstetrics and Gynecology",
    "ob": "Obstetrics and Gynecology",
    "obstetrics gynecology": "Obstetrics and Gynecology",
    "obstetrics and gynaecology": "Obstetrics and Gynecology",
    "womens health": "Obstetrics and Gynecology",
    "gen surg": "General Surgery",
    "surgery": "General Surgery",
    "general surg": "General Surgery",
    "ortho": "Orthopaedic Surgery",
    "orthopedic surgery": "Orthopaedic Surgery",
    "orthopedics": "Orthopaedic Surgery",
    "orthopaedics": "Orthopaedic Surgery",
    "neurosurgery": "Neurological Surgery",
    "neuro surgery": "Neurological Surgery",
    "neurologic surgery": "Neurological Surgery",
    "ent": "Otolaryngology",
    "otolaryngology head and neck surgery": "Otolaryngology",
    "head and neck surgery": "Otolaryngology",
    "psych": "Psychiatry",
    "psychiatry and behavioral sciences": "Psychiatry",
    "behavioral health": "Psychiatry",
    "neuro": "Neurology",
    "anesthesia": "Anesthesiology",
    "anaesthesiology": "Anesthesiology",
    "anesthesiology and critical care": "Anesthesiology",
    "derm": "Dermatology",
    "path": "Pathology",
    "pathology and laboratory medicine": "Pathology",
    "laboratory medicine": "Pathology",
    "rads": "Radiology",
    "diagnostic radiology": "Radiology",
    "radiology imaging": "Radiology",
    "imaging": "Radiology",
    "rad onc": "Radiation Oncology",
    "radonc": "Radiation Oncology",
    "radiation and cellular oncology": "Radiation Oncology",
    "radiation cellular oncology": "Radiation Oncology",
    "cellular oncology": "Radiation Oncology",
    "radiation medicine": "Radiation Oncology",
    "pmr": "Physical Medicine and Rehabilitation",
    "pm r": "Physical Medicine and Rehabilitation",
    "physiatry": "Physical Medicine and Rehabilitation",
    "rehabilitation medicine": "Physical Medicine and Rehabilitation",
    "optho": "Ophthalmology",
    "ophthalmology and visual sciences": "Ophthalmology",
    "eye": "Ophthalmology",
    "cards": "Cardiology",
    "cardiovascular medicine": "Cardiology",
    "cardiovascular disease": "Cardiology",
    "gi": "Gastroenterology",
    "gastroenterology and hepatology": "Gastroenterology",
    "heme onc": "Hematology and Oncology",
    "hematology oncology": "Hematology and Oncology",
    "hematology": "Hematology and Oncology",
    "oncology": "Hematology and Oncology",
    "medical oncology": "Hematology and Oncology",
    "id": "Infectious Disease",
    "infectious diseases": "Infectious Disease",
    "renal": "Nephrology",
    "kidney": "Nephrology",
    "rheum": "Rheumatology",
    "pccm": "Pulmonary and Critical Care",
    "pulmonary": "Pulmonary and Critical Care",
    "pulmonary critical care": "Pulmonary and Critical Care",
    "pulmonary and critical care medicine": "Pulmonary and Critical Care",
    "critical care": "Pulmonary and Critical Care",
    "ct surgery": "Thoracic Surgery",
    "cardiothoracic surgery": "Thoracic Surgery",
    "cardiac surgery": "Thoracic Surgery",
    "vascular": "Vascular Surgery",
    "ir": "Interventional Radiology",
    "omfs": "Oral and Maxillofacial Surgery",
    "prev med": "Preventive Medicine",
    "public health and preventive medicine": "Preventive Medicine",
    "occupational medicine": "Preventive Medicine",
    "genetics": "Medical Genetics",
    "medical genetics and genomics": "Medical Genetics",
    "nuc med": "Nuclear Medicine",
    "plastics": "Plastic Surgery",
    "plastic and reconstructive surgery": "Plastic Surgery",
    "child neuro": "Child Neurology",
    "pediatric neurology": "Child Neurology",
}

# Words that carry no specialty signal. Stripped before matching so that
# "Department of Internal Medicine Residency Program" reduces to "internal medicine".
_NOISE = {
    "department", "departments", "dept", "division", "divisions", "school",
    "college", "center", "centre", "institute", "program", "programs",
    "programme", "residency", "residencies", "resident", "residents",
    "fellowship", "fellowships", "fellow", "fellows", "training", "graduate",
    "medical", "education", "gme", "house", "staff", "housestaff", "our",
    "the", "of", "and", "at", "for", "current", "meet", "people", "team",
    "faculty", "directory", "profiles", "roster", "class", "university",
    "hospital", "health", "healthcare", "system", "clinic", "academic",
    "home", "page", "welcome", "about", "overview", "index",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _squash(text: str) -> str:
    """Lowercase, collapse everything non-alphanumeric to single spaces."""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def _strip_noise(squashed: str) -> str:
    return " ".join(w for w in squashed.split() if w not in _NOISE)


# Canonical forms keyed by their own squashed spelling, for exact hits.
_CANONICAL_BY_SQUASH = {_squash(name): name for name in CANONICAL_SPECIALTIES}
_CANONICAL_BY_DENOISED = {
    _strip_noise(_squash(name)): name for name in CANONICAL_SPECIALTIES
}


# Subdomain labels that name a whole medical school or health system rather than
# one program. `medicine.<univ>.edu` is the School of Medicine, not the Internal
# Medicine department, so it must not out-rank a specialty in the path.
_GENERIC_HOST_LABELS = frozenset({
    "www", "web", "medicine", "med", "meds", "medical", "health", "healthcare",
    "hospital", "hospitals", "clinic", "clinics", "school", "college",
    "university", "campus", "education", "edu", "gme", "residency", "students",
    "people", "directory", "portal", "intranet", "sites", "about",
})


@dataclass(frozen=True)
class SpecialtyMatch:
    canonical: str | None
    raw: str
    confidence: float


def normalize_specialty(raw: str | None) -> SpecialtyMatch:
    """Collapse a free-text specialty string onto the controlled vocabulary.

    Returns canonical=None when nothing matches, rather than guessing — an
    unmatched specialty is stored raw and shows up as unnormalized in admin.
    """
    if not raw or not raw.strip():
        return SpecialtyMatch(None, raw or "", 0.0)

    squashed = _squash(raw)
    if squashed in _CANONICAL_BY_SQUASH:
        return SpecialtyMatch(_CANONICAL_BY_SQUASH[squashed], raw, 1.0)
    if squashed in _ALIASES:
        return SpecialtyMatch(_ALIASES[squashed], raw, 0.95)

    denoised = _strip_noise(squashed)
    if not denoised:
        return SpecialtyMatch(None, raw, 0.0)
    if denoised in _CANONICAL_BY_DENOISED:
        return SpecialtyMatch(_CANONICAL_BY_DENOISED[denoised], raw, 0.9)
    if denoised in _ALIASES:
        return SpecialtyMatch(_ALIASES[denoised], raw, 0.85)

    # Longest-alias substring match, so "internal medicine residency program at X"
    # still resolves. Longest first to stop "medicine" beating "internal medicine".
    padded = f" {denoised} "
    best: tuple[int, str] | None = None
    for alias, canonical in _ALIASES.items():
        if f" {alias} " in padded and (best is None or len(alias) > best[0]):
            best = (len(alias), canonical)
    for squashed_canon, canonical in _CANONICAL_BY_SQUASH.items():
        denoised_canon = _strip_noise(squashed_canon)
        if (
            denoised_canon
            and f" {denoised_canon} " in padded
            and (best is None or len(denoised_canon) > best[0])
        ):
            best = (len(denoised_canon), canonical)
    if best:
        return SpecialtyMatch(best[1], raw, 0.75)

    return SpecialtyMatch(None, raw, 0.0)


def specialty_from_url(url: str) -> SpecialtyMatch:
    """Infer specialty from URL path segments, most specific segment first."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    # Subdomain often carries the program, e.g. surgery.hospital.edu
    host_labels = parts.hostname.split(".")[:-2] if parts.hostname else []
    for candidate in [*reversed(segments), *reversed(host_labels)]:
        match = normalize_specialty(candidate)
        if match.canonical:
            # Discount relative to a title match: a path token is weaker evidence.
            return SpecialtyMatch(match.canonical, candidate, match.confidence * 0.8)
    return SpecialtyMatch(None, url, 0.0)


def infer_specialty(
    *, page_title: str | None = None, url: str | None = None,
    breadcrumbs: list[str] | None = None, explicit: str | None = None,
) -> SpecialtyMatch:
    """Best specialty for a page, most specific evidence first.

    Order: an explicit per-person label, then a specialty in the URL path, then a
    departmental subdomain, then breadcrumbs, then the page title.

    Two orderings here are deliberate:
      * The path beats the subdomain, so medicine.<univ>.edu/pediatrics/residents
        is Pediatrics rather than the School of Medicine's generic "medicine".
      * The subdomain beats the page title, so a page on radonc.<univ>.edu titled
        "Radiation and Cellular Oncology" is not filed under the generic
        "oncology" alias as Hematology and Oncology.
    """
    from urllib.parse import urlsplit

    if explicit:
        match = normalize_specialty(explicit)
        if match.canonical:
            return match

    if url:
        parts = urlsplit(url)
        for segment in reversed([s for s in parts.path.split("/") if s]):
            match = normalize_specialty(segment)
            if match.canonical:
                return SpecialtyMatch(match.canonical, segment, match.confidence * 0.9)

        for label in (parts.hostname or "").split(".")[:-2]:
            if label.lower() in _GENERIC_HOST_LABELS:
                continue
            match = normalize_specialty(label)
            if match.canonical:
                return SpecialtyMatch(match.canonical, label, match.confidence * 0.95)

    for candidate in (*(breadcrumbs or []), page_title):
        if candidate:
            match = normalize_specialty(candidate)
            if match.canonical:
                return match

    return SpecialtyMatch(None, explicit or page_title or "", 0.0)
