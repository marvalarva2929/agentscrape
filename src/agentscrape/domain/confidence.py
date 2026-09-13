"""Confidence scoring for an extracted record.

Confidence answers "how much should the client trust this row before emailing
it", so it weights the things that make a row actionable: a real address, a real
name, and structure that was read rather than inferred.
"""

from __future__ import annotations

from ..db.enums import ExtractionMethod, FetchMode
from ..extraction.person import ExtractedPerson
from ..validation.email import institutional_match


def score_record(
    person: ExtractedPerson,
    *,
    site_host: str,
    extraction_method: ExtractionMethod,
    fetch_mode: FetchMode,
    page_score: float = 0.0,
) -> float:
    """Return a 0-1 confidence for one extracted person."""
    score = person.confidence  # the extractor's own structural confidence

    if person.email:
        score += 0.10
        if institutional_match(person.email, site_host):
            score += 0.10  # address on the institution's own domain
    # No penalty for a missing address: many programmes simply do not publish
    # them, and a name with a training year is still wanted.

    if person.full_name and len(person.full_name.split()) >= 2:
        score += 0.05
    else:
        score -= 0.15

    if person.pgy is not None or person.class_of is not None:
        score += 0.05  # a year corroborates that this is a trainee listing

    if person.position:
        score += 0.05  # the page stated a title, so this is a real listing

    if extraction_method is ExtractionMethod.KNOWN_PATH:
        score += 0.05  # a page with a track record of yielding real people
    if fetch_mode is FetchMode.BOTH:
        score += 0.03  # HTML structure and the rendered page agreed

    if page_score > 8:
        score += 0.02  # found on a page that looks strongly like a roster

    return round(max(0.0, min(1.0, score)), 3)
