"""Skip check: decide whether a site changed without scraping it properly.

Two tiers, both cheap, because the whole point is to avoid the expensive path:

  1. Content hashes of the known-good paths. All unchanged -> skip immediately,
     without parsing anything.
  2. Otherwise parse addresses out of those same pages and take the Jaccard
     similarity against the identity keys already stored for the site. At or
     above the run's threshold (default 0.90), the roster is assumed unchanged.

Tier 2 needs the site to have been scraped before. A site with no stored records
can never be skipped, which is the correct default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db.enums import SkipReason

log = logging.getLogger("agentscrape.skip")


@dataclass(frozen=True)
class SkipDecision:
    should_skip: bool
    similarity: float | None
    reason: SkipReason | None
    detail: str

    @property
    def reason_value(self) -> str | None:
        return str(self.reason) if self.reason else None


def jaccard(left: set[str], right: set[str]) -> float:
    """Overlap of two identity sets. Two empty sets are 0.0, not 1.0 — nothing
    known about a site is not evidence that it is unchanged."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def compare_fingerprints(
    previous: dict[str, str] | None, current: dict[str, str]
) -> bool:
    """True when every probed path returned byte-identical content.

    The comparison is anchored on the *current* probe set, not the stored one.
    The probe only re-fetches known-good paths, so requiring the stored
    fingerprint to be fully covered would mean a site whose earlier run touched
    more URLs than it promoted could never match, and tier one would never fire.
    Every probed URL must still have a stored hash, so a new path forces a scan.
    """
    if not previous or not current:
        return False
    if set(current) - set(previous):
        return False
    return all(previous[url] == current[url] for url in current)


def decide_skip(
    *,
    stored_identities: set[str],
    probe_identities: set[str],
    previous_fingerprint: dict[str, str] | None,
    current_fingerprint: dict[str, str],
    threshold: float,
    force_rescan: bool = False,
    probe_succeeded: bool = True,
) -> SkipDecision:
    """Decide whether to skip a site, always producing a score and a reason."""
    if force_rescan:
        return SkipDecision(False, None, None, "force_rescan requested")
    if not stored_identities:
        return SkipDecision(
            False, None, None, "no records stored for this site yet"
        )
    if not probe_succeeded:
        # A failed probe is not evidence of stability; scrape rather than assume.
        return SkipDecision(
            False, None, None, "known-path probe failed; cannot assess change"
        )

    if compare_fingerprints(previous_fingerprint, current_fingerprint):
        return SkipDecision(
            True, 1.0, SkipReason.UNCHANGED_FINGERPRINT,
            f"all {len(current_fingerprint)} known paths returned identical content",
        )

    similarity = jaccard(stored_identities, probe_identities)
    if similarity >= threshold:
        return SkipDecision(
            True, similarity, SkipReason.SIMILARITY_THRESHOLD,
            f"{similarity:.3f} of stored records still present "
            f"(threshold {threshold:.2f})",
        )
    return SkipDecision(
        False, similarity, None,
        f"similarity {similarity:.3f} below threshold {threshold:.2f}",
    )
