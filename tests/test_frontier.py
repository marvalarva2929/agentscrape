"""The work list has to grow as pages are read.

Sitemap discovery runs once, before anything is fetched, so it cannot see a page
that is only reachable by following a link. Several Arizona rosters were exactly
that, and the fix is to fold the links off each fetched page back into the
ranked list — without disturbing the cursor the extract loop reads with.
"""

from __future__ import annotations

from agentscrape.config import settings
from agentscrape.pipeline.nodes.extract import FRONTIER_MIN_SCORE, _merge_frontier

ALLOWED = {"arizona.edu"}
ROSTER = "https://medicine.arizona.edu/derm/residency-program/current-residents"


def candidate(url: str, score: float) -> dict:
    return {"url": url, "score": score, "is_known_path": False}


def test_a_roster_found_mid_crawl_is_queued_ahead_of_weaker_candidates() -> None:
    queued = [
        candidate("https://medicine.arizona.edu/a/faculty", 6.5),
        candidate("https://medicine.arizona.edu/b/our-team", 5.5),
    ]
    merged = _merge_frontier(queued, 0, {ROSTER: None}, allowed=ALLOWED)
    assert merged[0]["url"] == ROSTER


def test_visited_candidates_keep_their_positions() -> None:
    """The caller's cursor indexes this list, so the head must not move."""
    visited = [candidate(f"https://medicine.arizona.edu/seen/{i}", 9.0) for i in range(3)]
    queued = [candidate("https://medicine.arizona.edu/queued", 2.0)]
    merged = _merge_frontier(
        [*visited, *queued], len(visited), {ROSTER: None}, allowed=ALLOWED
    )
    assert [c["url"] for c in merged[:3]] == [c["url"] for c in visited]
    assert merged[3]["url"] == ROSTER


def test_links_already_on_the_list_are_not_duplicated() -> None:
    queued = [candidate(ROSTER, 17.0)]
    merged = _merge_frontier(queued, 0, {ROSTER: None}, allowed=ALLOWED)
    assert len(merged) == 1


def test_navigation_links_are_below_the_admission_floor() -> None:
    """Every page carries the department's own nav; admitting it would refill
    the list faster than the crawl drains it."""
    noise = {
        "https://medicine.arizona.edu/about/contact-us": None,
        "https://medicine.arizona.edu/education/apply": None,
        "https://medicine.arizona.edu/giving": None,
    }
    assert _merge_frontier([], 0, noise, allowed=ALLOWED) == []
    assert FRONTIER_MIN_SCORE > 0


def test_model_selected_link_bypasses_keyword_floor_and_moves_to_front() -> None:
    conceptual = "https://medicine.arizona.edu/education/people-we-serve"
    weak = candidate("https://medicine.arizona.edu/b/our-team", 5.5)
    merged = _merge_frontier(
        [weak], 0, {conceptual: None}, allowed=ALLOWED, preferred=[conceptual]
    )
    assert merged[0]["url"] == conceptual
    assert merged[0]["llm_selected"] is True


def test_links_off_the_institution_are_refused() -> None:
    offsite = {"https://acgme.org/residency-program/current-residents": None}
    assert _merge_frontier([], 0, offsite, allowed=ALLOWED) == []


def test_an_affiliated_domain_is_followed() -> None:
    """A medical centre spans the university and the health system it staffs.

    Chicago's trainees are published on uchicagomedicine.org while its GME site
    is uchicago.edu; scoping to the entry domain alone put 374 of its 377
    addresses permanently out of reach.
    """
    roster = "https://www.uchicagomedicine.org/gme/anesthesia/current-residents"
    merged = _merge_frontier(
        [], 0, {roster: None}, allowed={"uchicago.edu", "uchicagomedicine.org"}
    )
    assert [c["url"] for c in merged] == [roster]
    # And still refused when that domain is not one of the institution's.
    assert _merge_frontier([], 0, {roster: None}, allowed={"uchicago.edu"}) == []


def test_a_full_list_admits_a_better_page_by_displacing_a_worse_one() -> None:
    queued = [
        candidate(f"https://medicine.arizona.edu/pad/{i}", 6.5)
        for i in range(settings.max_candidates)
    ]
    merged = _merge_frontier(queued, 0, {ROSTER: None}, allowed=ALLOWED)
    assert len(merged) == settings.max_candidates
    assert merged[0]["url"] == ROSTER


def test_no_links_leaves_the_list_untouched() -> None:
    queued = [candidate(ROSTER, 17.0)]
    assert _merge_frontier(queued, 0, {}, allowed=ALLOWED) is queued


def test_href_entities_are_decoded_before_canonicalizing() -> None:
    """`&amp;` in an attribute is one separator, not a parameter called "amp".

    Canonicalizing it literally produced a distinct URL per link to the same
    page, and one faculty directory was fetched six times for the same people.
    """
    from agentscrape.discovery.sitemap import extract_links

    html = (
        '<a href="/directory?types=5&amp;sort=title_ASC&amp;page=0">a</a>'
        '<a href="/directory?page=0&amp;sort=title_ASC&amp;types=5">b</a>'
    )
    links = extract_links(html, "https://www.ttuhsc.edu/")
    assert links == ["https://www.ttuhsc.edu/directory?page=0&sort=title_ASC&types=5"]
