"""Departmental sites are siblings of the entry point, not children of it.

A medical school shares its registrable domain with the whole university, so the
host frontier has to reach `surgery.<univ>.edu` and `obgyn.<univ>.edu` while
leaving the bookstore and the CDN alone.
"""

from __future__ import annotations

import pytest

from agentscrape.pipeline.nodes.discover import _is_out_of_scope, _observed_hosts

ROOT = "medicine.arizona.edu"


def urls(*hosts: str) -> dict[str, None]:
    return {f"https://{h}/page-{i}": None for i, h in enumerate(hosts)}


@pytest.mark.parametrize(
    "host",
    [
        "surgery.arizona.edu", "obgyn.arizona.edu", "pathology.arizona.edu",
        "ortho.arizona.edu", "heart.arizona.edu", "eyes.arizona.edu",
        "arthritis.arizona.edu", "cancercenter.arizona.edu",
        "rad-onc.arizona.edu", "sportsmedicine.fcm.arizona.edu",
    ],
)
def test_clinical_hosts_are_in_scope(host: str) -> None:
    assert not _is_out_of_scope(host, ROOT)
    assert host in _observed_hosts(urls(host), ROOT)


@pytest.mark.parametrize(
    "host",
    [
        "cdn.digital.arizona.edu", "static.arizona.edu", "www.arizona.edu",
        "shop.arizona.edu", "map.arizona.edu", "news.arizona.edu",
        "talent.arizona.edu", "law.arizona.edu", "library.arizona.edu",
        "www.environment.arizona.edu",
    ],
)
def test_university_hosts_are_out_of_scope(host: str) -> None:
    assert _is_out_of_scope(host, ROOT)
    assert host not in _observed_hosts(urls(host), ROOT)


def test_the_entry_host_is_never_ruled_out_by_its_own_name() -> None:
    """A run rooted at a host on the deny-list is a different job, not this one."""
    assert not _is_out_of_scope("law.arizona.edu", "law.arizona.edu")


def test_the_entry_host_is_not_returned_for_a_second_walk() -> None:
    assert ROOT not in _observed_hosts(urls(ROOT, "obgyn.arizona.edu"), ROOT)


def test_hosts_outside_the_institution_are_ignored() -> None:
    assert _observed_hosts(urls("residents.acgme.org"), ROOT) == []


def test_departmental_hosts_are_walked_before_unlabelled_ones() -> None:
    """Sitemap walking is bounded, so the ordering decides what gets reached."""
    ranked = _observed_hosts(urls("cbc.arizona.edu", "obgyn.arizona.edu"), ROOT)
    assert ranked.index("obgyn.arizona.edu") < ranked.index("cbc.arizona.edu")


@pytest.mark.parametrize(
    "host,out_of_scope",
    [
        # A bare www in front of a department says nothing about the department.
        ("www.surgery.arizona.edu", False),
        ("www.obgyn.arizona.edu", False),
        # The university's own front page has no label of its own.
        ("www.arizona.edu", True),
        ("www.library.arizona.edu", True),
    ],
)
def test_a_leading_www_does_not_decide_scope(host: str, out_of_scope: bool) -> None:
    assert _is_out_of_scope(host, ROOT) is out_of_scope


def test_a_www_prefixed_department_still_ranks_as_a_department() -> None:
    """The scope check and the ranking must judge the same label."""
    from agentscrape.pipeline.nodes.discover import _rank_subdomains

    ranked = _rank_subdomains(
        ["cbc.arizona.edu", "www.surgery.arizona.edu"], ROOT
    )
    assert ranked and ranked[0] == "www.surgery.arizona.edu"


@pytest.mark.parametrize(
    "host,root,out_of_scope",
    [
        # A medical centre spans the university and the health system it staffs.
        # The affiliated domain's front page is its main site, and rejecting it
        # as a "www" host put every Chicago roster out of reach.
        ("www.uchicagomedicine.org", "gme.uchicago.edu", False),
        ("uchicagomedicine.org", "gme.uchicago.edu", False),
        # The entry point's own apex is still the whole university, not the
        # medical school, and walking it costs a great deal for nothing.
        ("www.arizona.edu", "medicine.arizona.edu", True),
        # Infrastructure on an affiliated domain stays out.
        ("cdn.uchicagomedicine.org", "gme.uchicago.edu", True),
    ],
)
def test_an_affiliated_domains_front_page_is_in_scope(host, root, out_of_scope) -> None:
    assert _is_out_of_scope(host, root) is out_of_scope
