"""School sheets: loose headers, required columns, idempotent upsert."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agentscrape.db.models import Site
from agentscrape.schools_sheet import load_school_sheets, parse_school_table

SAMPLE = [
    ["#", "Tier", "Institution", "Official Website", "Residency / Fellowship Hub",
     "School / People Directory", "Directory Type", "Crawler Fit", "Crawler Notes"],
    ["1", "A", "University of Arizona", "https://www.arizona.edu",
     "https://medicine.arizona.edu/education/residency-fellowship",
     "https://directory.arizona.edu", "Search form", "Good", "JS app"],
    ["2", "B", "Baylor College of Medicine", "bcm.edu", "https://www.bcmhospital.org/gme",
     "https://www.bcm.edu/people-search", "Search form", "", ""],
    ["3", "B", "No Directory U", "https://nodir.edu", "", "", "", "", ""],
]


def test_the_sample_sheet_is_read_by_header_meaning() -> None:
    rows = parse_school_table(SAMPLE)

    assert [r.name for r in rows] == [
        "University of Arizona", "Baylor College of Medicine", "No Directory U",
    ]
    arizona, bcm, no_directory = rows
    assert arizona.directory_url.startswith("https://directory.arizona.edu")
    # The hub is on the same institution, so crawling starts there.
    assert "medicine.arizona.edu" in arizona.entry_url
    assert arizona.affiliated_domains == []
    # A hub on another domain is still the entry point, and the website's
    # domain stays in scope beside it.
    assert "bcmhospital.org/gme" in bcm.entry_url
    assert bcm.affiliated_domains == ["bcm.edu"]
    assert no_directory.directory_url is None


def test_a_minimal_three_column_sheet_works() -> None:
    rows = parse_school_table([
        ["School", "URL", "Directory"],
        ["Texas Tech HSC", "https://www.ttuhsc.edu", "https://www.ttuhsc.edu/directory"],
    ])
    assert len(rows) == 1 and rows[0].entry_url.startswith("https://www.ttuhsc.edu")


def test_a_sheet_missing_a_required_column_is_refused() -> None:
    rows = parse_school_table([["Institution", "Website"], ["X", "https://x.edu"]])
    assert rows[0].directory_url is None


@pytest.mark.asyncio
async def test_loading_is_an_idempotent_upsert(session, tmp_path) -> None:
    sheet = tmp_path / "schools.csv"
    sheet.write_text(
        "Institution,Website,People Directory\n"
        "Example University,https://med.example.edu,https://directory.example.edu\n"
    )
    first = await load_school_sheets(session, tmp_path)
    second = await load_school_sheets(session, tmp_path)
    assert (first.created, second.created, second.unchanged) == (1, 0, 1)

    site = await session.scalar(select(Site).where(Site.root_domain == "med.example.edu"))
    assert site.name == "Example University"
    site.directory_config = {"mode": "get"}
    await session.commit()

    sheet.write_text(
        "Institution,Website,People Directory\n"
        "Example University,https://med.example.edu,https://people.example.edu/search\n"
    )
    third = await load_school_sheets(session, tmp_path)
    await session.refresh(site)
    assert third.updated == 1
    assert site.directory_url.startswith("https://people.example.edu")
    # A different directory has to be learned again.
    assert site.directory_config is None
