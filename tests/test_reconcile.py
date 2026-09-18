"""Reconciliation: versioning, diffs, missing detection.

These encode Section 3.5 of the brief. The frontend never derives what changed,
so the diff written here is the product feature.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from agentscrape.db.enums import ExtractionMethod, FetchMode, PersonCategory, RecordStatus
from agentscrape.db.models import Record, RecordVersion, Site
from agentscrape.db.repositories.records import (
    ExtractionContext,
    lock_site,
    mark_missing_records,
    reconcile_people,
    stored_identity_keys,
)
from agentscrape.extraction.person import ExtractedPerson
from agentscrape.urls import url_hash

ROSTER_URL = "https://med.example.edu/residents"


async def _make_site(session) -> Site:
    site = Site(
        root_domain="med.example.edu",
        canonical_url="https://med.example.edu/",
        hospital_name="Example Teaching Hospital",
    )
    session.add(site)
    await session.flush()
    return site


def _context(site: Site, **overrides) -> ExtractionContext:
    defaults = dict(
        site_id=site.id,
        site_host="med.example.edu",
        source_url=ROSTER_URL,
        page_title="Current Residents",
        extraction_method=ExtractionMethod.DISCOVERY,
        fetch_mode=FetchMode.HTML,
        run_id="run_1",
        site_run_id="sr_1",
    )
    defaults.update(overrides)
    return ExtractionContext(**defaults)


def _person(name="Jane Doe", email="jane@med.example.edu", **kw) -> ExtractedPerson:
    return ExtractedPerson(
        full_name=name,
        email=email,
        category=kw.pop("category", PersonCategory.RESIDENT),
        position=kw.pop("position", "Resident"),
        pgy=kw.pop("pgy", 2),
        class_of=kw.pop("class_of", None),
        **kw,
    )


class TestFirstRun:
    async def test_creates_record_and_first_version(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        result = await reconcile_people(session, [_person()], _context(site))
        await session.commit()

        assert (result.new, result.changed, result.unchanged) == (1, 0, 0)
        record = (await session.execute(select(Record))).scalar_one()
        assert record.email == "jane@med.example.edu"
        assert record.status == RecordStatus.NEW
        assert record.version_count == 1
        assert record.current_version_id is not None

        version = (await session.execute(select(RecordVersion))).scalar_one()
        assert version.version_no == 1
        assert version.changed_fields == {}
        assert version.source_url == ROSTER_URL
        assert version.page_title == "Current Residents"
        assert version.extraction_method == ExtractionMethod.DISCOVERY

    async def test_person_without_name_or_email_is_skipped(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        result = await reconcile_people(
            session, [ExtractedPerson(full_name=None, email=None)], _context(site)
        )
        assert result.skipped == 1 and result.new == 0


class TestSecondRun:
    async def test_identical_data_writes_no_new_version(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(session, [_person()], _context(site))
        await session.commit()

        later = datetime.now(UTC) + timedelta(days=1)
        result = await reconcile_people(
            session, [_person()], _context(site, captured_at=later, run_id="run_2")
        )
        await session.commit()

        assert (result.new, result.changed, result.unchanged) == (0, 0, 1)
        record = (await session.execute(select(Record))).scalar_one()
        assert record.version_count == 1
        assert record.status == RecordStatus.ACTIVE
        assert record.last_seen_at.replace(tzinfo=UTC) >= later.replace(microsecond=0)
        versions = (await session.execute(select(RecordVersion))).scalars().all()
        assert len(versions) == 1

    async def test_changed_field_writes_a_version_with_the_diff(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(session, [_person(name="Jane Doe")], _context(site))
        await session.commit()

        result = await reconcile_people(
            session, [_person(name="Jane Smith")], _context(site, run_id="run_2")
        )
        await session.commit()

        assert (result.new, result.changed) == (0, 1)
        record = (await session.execute(select(Record))).scalar_one()
        assert record.full_name == "Jane Smith"
        assert record.status == RecordStatus.CHANGED
        assert record.version_count == 2

        versions = (
            await session.execute(
                select(RecordVersion).order_by(RecordVersion.version_no)
            )
        ).scalars().all()
        assert len(versions) == 2
        assert versions[1].changed_fields == {
            "full_name": {"from": "Jane Doe", "to": "Jane Smith"}
        }
        assert record.current_version_id == versions[1].id

    async def test_same_email_different_name_is_one_record(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(session, [_person(name="Jane Doe")], _context(site))
        await reconcile_people(session, [_person(name="Jane Smith")], _context(site))
        await session.commit()
        records = (await session.execute(select(Record))).scalars().all()
        assert len(records) == 1

    async def test_annual_pgy_rollover_is_not_a_change(self, session):
        # The stored value is what the page said; only a *different published*
        # PGY counts. Otherwise every July would flag the whole dataset.
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(
            session, [_person(pgy=2)],
            _context(site, captured_at=datetime(2025, 9, 1, tzinfo=UTC)),
        )
        await session.commit()
        result = await reconcile_people(
            session, [_person(pgy=2)],
            _context(site, captured_at=datetime(2026, 9, 1, tzinfo=UTC), run_id="run_2"),
        )
        assert result.unchanged == 1 and result.changed == 0

    async def test_missing_field_does_not_erase_stored_value(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(session, [_person(class_of=2027)], _context(site))
        await session.commit()
        result = await reconcile_people(
            session, [_person(class_of=None)], _context(site, run_id="run_2")
        )
        await session.commit()
        record = (await session.execute(select(Record))).scalar_one()
        assert record.class_of == 2027
        assert result.unchanged == 1


class TestMissing:
    async def test_absent_from_a_revisited_page_is_marked_missing(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(
            session, [_person(name="Jane Doe", email="jane@med.example.edu"),
                      _person(name="Bob Roe", email="bob@med.example.edu")],
            _context(site),
        )
        await session.commit()

        result = await reconcile_people(
            session, [_person(name="Jane Doe")], _context(site, run_id="run_2")
        )
        marked = await mark_missing_records(
            session, site_id=site.id, seen_record_ids=set(result.record_ids),
            visited_url_hashes={url_hash(ROSTER_URL)},
        )
        await session.commit()

        assert marked == 1
        bob = (
            await session.execute(
                select(Record).where(Record.email == "bob@med.example.edu")
            )
        ).scalar_one()
        assert bob.status == RecordStatus.MISSING
        assert bob.missing_since is not None

    async def test_unvisited_page_does_not_mark_anyone_missing(self, session):
        # A discovery miss or a 500 must not look like someone leaving.
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(session, [_person()], _context(site))
        await session.commit()

        marked = await mark_missing_records(
            session, site_id=site.id, seen_record_ids=set(),
            visited_url_hashes={url_hash("https://med.example.edu/somewhere-else")},
        )
        assert marked == 0

    async def test_records_are_never_deleted(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(session, [_person()], _context(site))
        await session.commit()
        await mark_missing_records(
            session, site_id=site.id, seen_record_ids=set(),
            visited_url_hashes={url_hash(ROSTER_URL)},
        )
        await session.commit()
        assert len((await session.execute(select(Record))).scalars().all()) == 1

    async def test_returning_record_becomes_active_again(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(session, [_person()], _context(site))
        await session.commit()
        await mark_missing_records(
            session, site_id=site.id, seen_record_ids=set(),
            visited_url_hashes={url_hash(ROSTER_URL)},
        )
        await session.commit()

        await reconcile_people(session, [_person()], _context(site, run_id="run_3"))
        await session.commit()
        record = (await session.execute(select(Record))).scalar_one()
        assert record.status == RecordStatus.ACTIVE
        assert record.missing_since is None


class TestSkipInputs:
    async def test_stored_identities_exclude_missing_records(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)
        await reconcile_people(
            session, [_person(name="Jane Doe", email="jane@med.example.edu"),
                      _person(name="Bob Roe", email="bob@med.example.edu")],
            _context(site),
        )
        await session.commit()
        assert len(await stored_identity_keys(session, site.id)) == 2

        await mark_missing_records(
            session, site_id=site.id, seen_record_ids=set(),
            visited_url_hashes={url_hash(ROSTER_URL)},
        )
        await session.commit()
        assert await stored_identity_keys(session, site.id) == set()


class TestProvenance:
    async def test_version_carries_screenshot_and_field_locations(self, session):
        site = await _make_site(session)
        expires = datetime.now(UTC) + timedelta(days=90)
        person = _person()
        person.locate_hints = ["Jane Doe", "jane@med.example.edu"]
        await lock_site(session, site.id)
        await reconcile_people(
            session, [person],
            _context(
                site,
                screenshot_path="screenshots/sr_1/abc.png",
                screenshot_expires_at=expires,
                fetch_mode=FetchMode.BOTH,
                field_locations={
                    "Jane Doe": {"x": 10, "y": 20, "width": 100, "height": 18},
                    "jane@med.example.edu": {"x": 10, "y": 40, "width": 180, "height": 16},
                },
            ),
        )
        await session.commit()

        version = (await session.execute(select(RecordVersion))).scalar_one()
        assert version.screenshot_available is True
        assert version.screenshot_expires_at is not None
        assert version.field_locations["full_name"]["y"] == 20
        assert version.field_locations["email"]["x"] == 10
        assert version.fetch_mode == FetchMode.BOTH


class TestJsonNullSemantics:
    """A cleared JSON column must be SQL NULL, not the JSON value `null`.

    Without `none_as_null`, SQLAlchemy writes Python None into a JSON column as
    JSON `null`, so `IS NULL` never matches and a cleared checkpoint still looks
    present to any query that filters on it.
    """

    async def test_cleared_json_column_is_sql_null(self, session):
        from agentscrape.db.models import Run, SiteRun

        site = await _make_site(session)
        run = Run(status="running", sites_total=1)
        session.add(run)
        await session.flush()
        site_run = SiteRun(run_id=run.id, site_id=site.id, status="running")
        session.add(site_run)
        await session.commit()

        from sqlalchemy import update

        await session.execute(
            update(SiteRun)
            .where(SiteRun.id == site_run.id)
            .values(checkpoint_state={"cursor": 3})
        )
        await session.commit()
        assert not await session.scalar(
            select(SiteRun.checkpoint_state.is_(None)).where(SiteRun.id == site_run.id)
        )

        await session.execute(
            update(SiteRun)
            .where(SiteRun.id == site_run.id)
            .values(checkpoint_state=None)
        )
        await session.commit()
        assert await session.scalar(
            select(SiteRun.checkpoint_state.is_(None)).where(SiteRun.id == site_run.id)
        )


class TestUnknownNeverOverwritesAKnownCategory:
    """An institution-wide directory identifies nobody precisely.

    Every card on one prints a single combined "Resident/Fellow" term, which
    yields `unknown` — while the same people appear on their own departmental
    roster labelled exactly. Plain last-write-wins let whichever page was fetched
    last decide, and the directory is usually last because it ranks lower. That
    blanked the category on 86 people the client's own sheet had confirmed.
    """

    async def test_a_later_unknown_leaves_the_known_category_alone(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)

        await reconcile_people(
            session,
            [_person(name="Abbey Bayless", email="ab@med.example.edu")],
            _context(site, source_url="https://med.example.edu/anesthesia/current-residents"),
        )
        await reconcile_people(
            session,
            [
                _person(
                    name="Abbey Bayless",
                    email="ab@med.example.edu",
                    category=PersonCategory.UNKNOWN,
                    position="Resident/Fellow",
                    pgy=None,
                )
            ],
            _context(
                site,
                source_url="https://med.example.edu/our-team-leadership",
                page_title="Our Team Leadership",
            ),
        )
        await session.commit()

        record = (
            await session.execute(
                select(Record).where(Record.email == "ab@med.example.edu")
            )
        ).scalar_one()
        assert record.category == str(PersonCategory.RESIDENT)

    async def test_a_real_category_still_replaces_an_earlier_unknown(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)

        await reconcile_people(
            session,
            [
                _person(
                    name="Ajay Kerai",
                    email="ak@med.example.edu",
                    category=PersonCategory.UNKNOWN,
                    position=None,
                    pgy=None,
                )
            ],
            _context(site, source_url="https://med.example.edu/our-team-leadership"),
        )
        await reconcile_people(
            session,
            [
                _person(
                    name="Ajay Kerai",
                    email="ak@med.example.edu",
                    category=PersonCategory.FELLOW,
                    position="Fellow",
                    pgy=5,
                )
            ],
            _context(site, source_url="https://med.example.edu/cardiology/fellows"),
        )
        await session.commit()

        record = (
            await session.execute(
                select(Record).where(Record.email == "ak@med.example.edu")
            )
        ).scalar_one()
        assert record.category == str(PersonCategory.FELLOW)


class TestAnAddressAdoptsTheRecordBuiltWithoutOne:
    """Rosters and directories disagree about addresses.

    A departmental roster names its residents and publishes no addresses; the
    institution-wide directory has the addresses but prints one combined
    "Resident/Fellow" term for everyone. Keyed separately, one person became two
    records — the right category on one, the address on the other. On Arizona
    that was 238 people, and it is why residents the client's own sheet had
    confirmed kept reading back as `unknown`.
    """

    async def test_the_two_sightings_become_one_record(self, session):
        site = await _make_site(session)
        await lock_site(session, site.id)

        # The roster knows what she is, but not how to reach her.
        await reconcile_people(
            session,
            [_person(name="Coen Hasenkamp", email=None, position="Resident")],
            _context(site, source_url="https://med.example.edu/obgyn/current-residents"),
        )
        # The directory knows how to reach her, but calls everyone the same thing.
        await reconcile_people(
            session,
            [
                _person(
                    name="Coen Hasenkamp",
                    email="ch@med.example.edu",
                    category=PersonCategory.UNKNOWN,
                    position="Resident/Fellow",
                    pgy=None,
                )
            ],
            _context(site, source_url="https://med.example.edu/our-team-leadership"),
        )
        await session.commit()

        records = (
            await session.execute(
                select(Record).where(Record.full_name == "Coen Hasenkamp")
            )
        ).scalars().all()
        assert len(records) == 1
        record = records[0]
        assert record.email == "ch@med.example.edu"
        assert record.identity_key == "email:ch@med.example.edu"
        # The roster's category survives the directory's ambiguity.
        assert record.category == str(PersonCategory.RESIDENT)

    async def test_a_record_that_already_has_an_address_is_left_alone(self, session):
        """Two people with one name, each with their own address, stay separate."""
        site = await _make_site(session)
        await lock_site(session, site.id)

        await reconcile_people(
            session,
            [_person(name="Jane Doe", email="jane.a@med.example.edu")],
            _context(site),
        )
        await reconcile_people(
            session,
            [_person(name="Jane Doe", email="jane.b@med.example.edu")],
            _context(site, source_url="https://med.example.edu/other"),
        )
        await session.commit()

        records = (
            await session.execute(
                select(Record).where(Record.full_name == "Jane Doe")
            )
        ).scalars().all()
        assert len(records) == 2
