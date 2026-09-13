"""Skip-check semantics (Section 3.1).

Skipping is the main compute saving in the system, but a wrong skip silently
returns stale data, so the bias is always toward scanning.
"""

from __future__ import annotations

import pytest

from agentscrape.db.enums import SkipReason
from agentscrape.pipeline.fingerprint import compare_fingerprints, decide_skip, jaccard

STORED = {f"email:p{i}@x.edu" for i in range(10)}


class TestJaccard:
    def test_identical_sets(self):
        assert jaccard(STORED, STORED) == 1.0

    def test_empty_is_zero_not_one(self):
        # Knowing nothing about a site is not evidence that it is unchanged.
        assert jaccard(set(), set()) == 0.0
        assert jaccard(STORED, set()) == 0.0

    def test_partial_overlap(self):
        assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)


class TestFingerprint:
    def test_unchanged_probe_subset_matches(self):
        # The probe re-fetches only known-good paths, so it is a subset of what
        # the last run touched. Requiring full coverage would never match.
        previous = {"u1": "h1", "u2": "h2", "u3": "h3"}
        assert compare_fingerprints(previous, {"u1": "h1"}) is True

    def test_changed_content_does_not_match(self):
        assert compare_fingerprints({"u1": "h1"}, {"u1": "different"}) is False

    def test_a_probe_url_with_no_stored_hash_forces_a_scan(self):
        assert compare_fingerprints({"u1": "h1"}, {"u_new": "h9"}) is False

    def test_missing_either_side_forces_a_scan(self):
        assert compare_fingerprints(None, {"u1": "h1"}) is False
        assert compare_fingerprints({"u1": "h1"}, {}) is False


class TestDecideSkip:
    def _decide(self, **overrides):
        kwargs = dict(
            stored_identities=STORED, probe_identities=STORED,
            previous_fingerprint={"u1": "h1"}, current_fingerprint={"u1": "h1"},
            threshold=0.90,
        )
        kwargs.update(overrides)
        return decide_skip(**kwargs)

    def test_unchanged_content_skips_on_tier_one(self):
        decision = self._decide()
        assert decision.should_skip
        assert decision.reason is SkipReason.UNCHANGED_FINGERPRINT

    def test_changed_content_but_same_people_skips_on_similarity(self):
        decision = self._decide(current_fingerprint={"u1": "changed"})
        assert decision.should_skip
        assert decision.reason is SkipReason.SIMILARITY_THRESHOLD
        assert decision.similarity == 1.0

    def test_below_threshold_scans(self):
        decision = self._decide(
            current_fingerprint={"u1": "changed"},
            probe_identities={"email:p0@x.edu"},
        )
        assert not decision.should_skip
        assert decision.similarity == pytest.approx(0.1)

    def test_threshold_is_configurable(self):
        half = {f"email:p{i}@x.edu" for i in range(5)}
        assert not self._decide(
            current_fingerprint={"u1": "c"}, probe_identities=half, threshold=0.90
        ).should_skip
        assert self._decide(
            current_fingerprint={"u1": "c"}, probe_identities=half, threshold=0.40
        ).should_skip

    def test_force_rescan_always_wins(self):
        decision = self._decide(force_rescan=True)
        assert not decision.should_skip
        assert "force_rescan" in decision.detail

    def test_a_site_with_no_stored_records_is_never_skipped(self):
        decision = self._decide(stored_identities=set())
        assert not decision.should_skip

    def test_a_failed_probe_is_not_evidence_of_stability(self):
        # A 500 or a timeout must not be read as "nothing changed".
        decision = self._decide(probe_succeeded=False, current_fingerprint={})
        assert not decision.should_skip
        assert "probe failed" in decision.detail

    def test_every_decision_carries_a_reason(self):
        for decision in (
            self._decide(),
            self._decide(force_rescan=True),
            self._decide(stored_identities=set()),
            self._decide(current_fingerprint={"u1": "c"}, probe_identities=set()),
        ):
            assert decision.detail
