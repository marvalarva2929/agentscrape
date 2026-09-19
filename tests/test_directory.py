"""Directory search: learn the search once, fill only blanks, never guess."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from agentscrape.db.enums import ExtractionMethod, FetchMode, PersonCategory, RecordStatus
from agentscrape.db.models import Record, RecordVersion, Site
from agentscrape.db.repositories.records import ExtractionContext, reconcile_people
from agentscrape.db.repositories.sites import upsert_site
from agentscrape.directory.learn import find_search_forms, looks_like_login
from agentscrape.directory.lookup import same_person, unique_match
from agentscrape.domain.schemas import RunConfigIn, RunCreate
from agentscrape.extraction.person import ExtractedPerson
from agentscrape.orchestrator.service import DirectorySearchUnavailable, create_run

from .fixture_server import DIRECTORY, PRIVATE_DIRECTORY, serve
from .test_end_to_end import _run_once, fast_and_offline  # noqa: F401  (autouse fixture)


def test_the_directory_form_beats_the_site_search_in_the_header() -> None:
    forms = find_search_forms(DIRECTORY, "https://x.edu/directory")
    best = forms[0]
    assert best.action == "https://x.edu/directory/search"
    assert best.fields == {"query": "q"}
    assert best.template() == "https://x.edu/directory/search?scope=all&q={query}"


def test_a_sign_in_page_is_recognized() -> None:
    assert looks_like_login("https://x.edu/private-directory", PRIVATE_DIRECTORY)
    assert looks_like_login("https://sso.x.edu/idp/profile", "")
    assert not looks_like_login("https://x.edu/directory", DIRECTORY)


def test_a_query_echoed_back_is_not_a_result() -> None:
    from agentscrape.directory.learn import echo_count, names_the_person

    echoed = "<h1>Search for Dan Nobody</h1><p>Your search yielded no results.</p>"
    assert echo_count("<h1>Search for Qazwix Vornebly</h1>") == 1
    assert not names_the_person(echoed, "Dan Nobody", echo=1)
    found = "<h1>Search for Ann Riley</h1><div>Riley, Ann - Resident</div>"
    assert names_the_person(found, "Ann Riley", echo=1)


def test_matching_ignores_credentials_and_middle_initials_but_not_ambiguity() -> None:
    assert same_person("Riley, Ann M., MD", "Ann Riley")
    assert not same_person("Ann Rileigh", "Ann Riley")
    twins = [ExtractedPerson(full_name="Ben Cole", email="a@x.edu"),
             ExtractedPerson(full_name="Ben Cole", email="b@x.edu")]
    assert unique_match(twins, "Ben Cole") is False
    assert unique_match(twins, "Ann Riley") is None


async def _seed(session, base: str, directory_path: str) -> Site:
    site = await upsert_site(session, base)
    site.directory_url = f"{base}{directory_path}"
    people = [
        ExtractedPerson(full_name="Ann Riley", category=PersonCategory.RESIDENT,
                        position="Chief Resident"),
        ExtractedPerson(full_name="Cara Diaz", category=PersonCategory.RESIDENT),
        ExtractedPerson(full_name="Ben Cole", category=PersonCategory.RESIDENT),
        ExtractedPerson(full_name="Dan Nobody", category=PersonCategory.RESIDENT),
    ]
    await reconcile_people(session, people, ExtractionContext(
        site_id=site.id, site_host=site.root_domain, source_url=f"{base}/residents",
        page_title="Residents", extraction_method=ExtractionMethod.DISCOVERY,
        fetch_mode=FetchMode.HTML, captured_at=datetime.now(UTC),
    ))
    await session.commit()
    return site


async def _people(session, site_id: str) -> dict[str, Record]:
    rows = (await session.execute(select(Record).where(Record.site_id == site_id))).scalars()
    return {r.full_name: r for r in rows}


async def test_directory_only_run_fills_blanks_and_nothing_else(session):
    with serve(hostname="dir.localhost") as fixture:
        site = await _seed(session, fixture.base, "/directory")
        site_id = site.id
        await _run_once([fixture.base], session=session, modes=["directory"])

    session.expire_all()
    people = await _people(session, site_id)
    ann, cara, ben = people["Ann Riley"], people["Cara Diaz"], people["Ben Cole"]
    assert ann.email == "ann.riley@example.edu"
    # The roster's title wins over the directory's.
    assert ann.position == "Chief Resident"
    # Only on her profile page, one link away from the results.
    assert cara.email == "cara.diaz@example.edu"
    # Two Ben Coles: no address is better than the wrong one.
    assert ben.email is None
    assert all(p.status != RecordStatus.MISSING for p in people.values())

    method = await session.scalar(
        select(RecordVersion.extraction_method)
        .where(RecordVersion.record_id == ann.id)
        .order_by(RecordVersion.version_no.desc())
    )
    assert method == ExtractionMethod.DIRECTORY
    site = await session.get(Site, site_id)
    assert site.directory_config["mode"] == "get"
    assert "/directory/search" in site.directory_config["template"]
    assert site.directory_config["echo"] == 1


async def test_a_private_directory_is_reported_not_guessed_at(session):
    with serve(hostname="private.localhost") as fixture:
        site = await _seed(session, fixture.base, "/private-directory")
        site_id = site.id
        await _run_once([fixture.base], session=session, modes=["directory"])

    session.expire_all()
    site = await session.get(Site, site_id)
    assert site.directory_config["mode"] == "unavailable"
    assert "sign" in site.directory_config["reason"]
    assert all(p.email is None for p in (await _people(session, site_id)).values())


async def test_directory_search_is_refused_on_an_uncrawled_school(session):
    with pytest.raises(DirectorySearchUnavailable) as caught:
        await create_run(session, RunCreate(
            sites=["https://never-crawled.example.edu"],
            config=RunConfigIn(modes=["directory"]),
        ))
    assert "not crawled" in str(caught.value)


async def test_crawl_then_directory_in_one_run(session):
    with serve(hostname="both.localhost") as fixture:
        site = await upsert_site(session, fixture.base)
        site.directory_url = f"{fixture.base}/directory"
        site_id = site.id
        await session.commit()
        await _run_once([fixture.base], session=session, modes=["crawl", "directory"])

    session.expire_all()
    people = await _people(session, site_id)
    # The crawl found everyone; the roster already gave addresses, so the
    # directory had only years to add and never changes an address.
    assert people["Ann Riley"].email == "ann.riley@example.edu"
    site = await session.get(Site, site_id)
    assert site.directory_config is not None
