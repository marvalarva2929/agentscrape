"""Ranking has to separate a roster from the pages that merely mention one.

Every case here is a real URL from the Arizona scrape that the previous scorer
placed wrongly, which is what held that run to half the target roster.
"""

from __future__ import annotations

import pytest

from agentscrape.discovery.scoring import rank_candidates, score_url

ROSTERS = [
    "https://medicine.arizona.edu/pediatrics/residency-program/current-and-past-residents",
    "https://medicine.arizona.edu/dermatology/education/advanced-dermatology-residency-program/current-and-past-residents",
    "https://medicine.arizona.edu/emergencymed/residency-programs-overview/university-current-residents",
    "https://obgyn.arizona.edu/education/residency-program/current-residents",
    "https://pathology.arizona.edu/education/residency/current-residents",
    "https://surgery.arizona.edu/residencies-fellowships/plastic-surgery-residency-program/resident-profiles",
    "https://obgyn.arizona.edu/education/fellowship-program/meet-our-fellows",
    "https://medicine.arizona.edu/radiology/all-trainees",
]
# Pages that name people but are not rosters, or name a roster but are not one.
NON_ROSTERS = [
    "https://medicine.arizona.edu/education/residencies-fellowships/prospective-residents-fellows",
    "https://ortho.arizona.edu/news/residents-attended-ao-course",
    "https://news.arizona.edu/uannounce/natalia-santos-among-2019-class-e-kika-de-la-garza-fellows",
    "https://healthsciences.arizona.edu/connect/photos/college-medicine-phoenix-celebrates-class-2025",
    "https://medicine.arizona.edu/deptmedicine/education/fellowships/infectious-diseases-fellowship-program",
    "https://medicine.arizona.edu/about/contact-us",
]


@pytest.mark.parametrize("url", ROSTERS)
def test_roster_pages_clear_the_roster_band(url: str) -> None:
    assert score_url(url).score >= 14.0


@pytest.mark.parametrize("url", NON_ROSTERS)
def test_non_roster_pages_stay_below_the_roster_band(url: str) -> None:
    assert score_url(url).score < 14.0


def test_every_roster_outranks_every_non_roster() -> None:
    """The bands must not overlap: the step budget is spent in rank order."""
    assert min(score_url(u).score for u in ROSTERS) > max(
        score_url(u).score for u in NON_ROSTERS
    )


def test_and_past_infix_does_not_break_the_current_roster_signal() -> None:
    """"current-and-past-residents" is the commonest roster spelling on .edu sites.

    The previous scorer matched phrases against the whole path, so the "and-past"
    infix broke "current-residents" and the page scored as a bare "/residents".
    """
    with_infix = score_url(
        "https://x.edu/peds/residency-program/current-and-past-residents"
    )
    without = score_url("https://x.edu/peds/residency-program/current-residents")
    assert with_infix.score == without.score


def test_school_wide_subdomain_earns_no_specialty_bonus() -> None:
    """`medicine.<univ>.edu` is the whole school, not the medicine department.

    Awarding it the departmental bonus gave the same score to all 6,000 pages on
    the host, lifting every one of them over the candidate floor.
    """
    assert score_url("https://medicine.arizona.edu/some/unrelated/page").score <= 0.0
    assert score_url("https://obgyn.arizona.edu/education/residency-program").score > 0.0


def test_news_section_vetoes_a_roster_shaped_leaf() -> None:
    """A department's news feed is full of leaves ending in "residents"."""
    assert score_url("https://ortho.arizona.edu/news/congratulations-our-residents").score < 0


def test_article_slug_is_not_a_page_name() -> None:
    """A roster page is named in a few words; past that the leaf is a headline."""
    assert score_url(
        "https://x.edu/dept/natalia-santos-among-the-2019-class-of-garza-fellows"
    ).score < 8.0


def test_ranking_keeps_known_paths_first_and_respects_the_floor() -> None:
    ranked = rank_candidates(
        [*NON_ROSTERS, *ROSTERS],
        known_paths={NON_ROSTERS[-1]: 3.0},
        min_score=1.0,
    )
    assert ranked[0].url == NON_ROSTERS[-1]
    assert ranked[0].is_known_path
    kept = {r.url for r in ranked}
    assert set(ROSTERS) <= kept
