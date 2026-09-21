"""The client's 23 schools load, match what is stored, and hide the rest."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agentscrape.db.models import Site
from agentscrape.school_catalog import EXPECTED_SCHOOLS, import_catalog, load_catalog


def test_shipped_catalog_has_exactly_23_unique_valid_schools():
    schools = load_catalog()
    assert len(schools) == EXPECTED_SCHOOLS
    assert len({school.host for school in schools}) == EXPECTED_SCHOOLS
    assert all(s.name and s.entry_url.startswith("https://") for s in schools)


def test_every_school_starts_from_a_page_that_lists_programs():
    """A bare home page was the entry for eleven of these; a hub is the entry now."""
    from urllib.parse import urlsplit

    for school in load_catalog():
        # A dedicated GME site (gme.dartmouth-hitchcock.org) is a hub at its root.
        parts = urlsplit(school.entry_url)
        assert parts.path.strip("/") or parts.netloc.startswith("gme."), school.name


def test_the_benchmarked_schools_keep_the_hosts_their_data_is_stored_under():
    hosts = {s.name: s.host for s in load_catalog()}
    assert hosts["University of Chicago Medical Center"] == "gme.uchicago.edu"
    assert hosts["Baylor College of Medicine"] == "www.bcm.edu"
    assert hosts["Baylor Scott & White (Dallas/DFW/Temple/RR)"] == "www.bswhealth.com"
    assert hosts["Texas Tech University Health Sciences Center"] == "www.ttuhsc.edu"
    assert hosts["University of Arizona College of Medicine–Tucson"] == "medicine.arizona.edu"


def test_schools_that_publish_on_another_site_bring_it_into_scope():
    by_name = {s.name: s for s in load_catalog()}
    assert "uchicagomedicine.org" in by_name["University of Chicago Medical Center"].scope_domains
    assert "uvahealth.com" in by_name["University of Virginia Medical Center"].scope_domains
    # The entry's own domain is never listed as an extra one.
    assert "bcm.edu" not in by_name["Baylor College of Medicine"].scope_domains


class TestImport:
    async def test_it_activates_exactly_the_catalog_and_hides_everything_else(self, session):
        session.add(Site(root_domain="elsewhere.example.edu", canonical_url="https://elsewhere.example.edu/", name="Elsewhere"))
        await session.commit()

        result = await import_catalog(session)

        assert result["active"] == EXPECTED_SCHOOLS and result["failed"] == 0
        sites = (await session.execute(select(Site))).scalars().all()
        assert sum(1 for s in sites if s.is_active) == EXPECTED_SCHOOLS
        elsewhere = next(s for s in sites if s.root_domain == "elsewhere.example.edu")
        assert elsewhere.is_active is False  # hidden, not deleted

    async def test_a_school_already_stored_keeps_its_row_and_history(self, session):
        existing = Site(
            root_domain="gme.uchicago.edu", canonical_url="https://gme.uchicago.edu/programs/",
            name="UChicago", directory_url="https://directory.uchicago.edu/",
        )
        session.add(existing)
        await session.commit()
        original_id = existing.id

        await import_catalog(session)
        await session.refresh(existing)

        assert existing.id == original_id
        assert existing.name == "University of Chicago Medical Center"
        assert existing.is_active is True
        assert "uchicagomedicine.org" in (existing.affiliated_domains or [])

    async def test_running_it_twice_changes_nothing(self, session):
        await import_catalog(session)
        first = {s.root_domain: (s.name, s.canonical_url, s.is_active)
                 for s in (await session.execute(select(Site))).scalars()}
        await import_catalog(session)
        second = {s.root_domain: (s.name, s.canonical_url, s.is_active)
                  for s in (await session.execute(select(Site))).scalars()}
        assert first == second and len(second) == EXPECTED_SCHOOLS

    async def test_a_blank_directory_never_erases_one_already_known(self, session):
        await import_catalog(session)
        tower = (await session.execute(select(Site).where(Site.root_domain == "towerhealth.org"))).scalar_one()
        tower.directory_url = "https://towerhealth.org/known-directory"
        await session.commit()
        await import_catalog(session)
        await session.refresh(tower)
        # The catalog names a directory for Tower Health, so it wins; a school
        # with none listed must keep what it had.
        vcu = (await session.execute(select(Site).where(Site.root_domain == "medschool.vcu.edu"))).scalar_one()
        vcu.directory_url = "https://kept.example.edu/"
        await session.commit()
        await import_catalog(session)
        await session.refresh(vcu)
        assert vcu.directory_url in ("https://kept.example.edu/", "https://phonebook.vcu.edu/")

    async def test_a_broken_catalog_is_refused_and_hides_nothing(self, session, tmp_path):
        session.add(Site(root_domain="keep.example.edu", canonical_url="https://keep.example.edu/", name="Keep"))
        await session.commit()
        bad = tmp_path / "bad.csv"
        bad.write_text("name,entry_url\nOnly One,https://one.example.edu/gme\n")

        with pytest.raises(ValueError, match="exactly 23"):
            await import_catalog(session, path=bad)

        keep = (await session.execute(select(Site).where(Site.root_domain == "keep.example.edu"))).scalar_one()
        assert keep.is_active is True

    async def test_a_dry_run_writes_nothing(self, session):
        await import_catalog(session, dry_run=True)
        assert (await session.execute(select(Site))).scalars().all() == []
