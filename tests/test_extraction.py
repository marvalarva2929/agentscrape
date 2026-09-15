"""HTML extraction and the decision to escalate to a browser."""

from __future__ import annotations

import pytest

from agentscrape.db.enums import PersonCategory
from agentscrape.extraction.html_people import (
    _extract_name,
    _extract_position,
    classify_person,
    extract_people,
    page_is_alumni_listing,
    page_looks_thin,
)
from agentscrape.validation.email import extract_emails, is_plausible

from .fixtures import (
    ALUMNI_ROSTER,
    CARD_ROSTER,
    HEADING_ONLY_FACULTY,
    MIXED_DIRECTORY,
    NO_EMAIL_ROSTER,
    SPA_SHELL,
    TABLE_ROSTER,
    large_card_roster,
)


class TestNameExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Jane A. Doe, MD PGY-2", "Jane A. Doe"),
            ("Tom O'Brien Fellow, Class of '28", "Tom O'Brien"),
            ("Sean McDonald-Smith", "Sean McDonald-Smith"),
            ("Resident Jane Smith PGY-1", "Jane Smith"),
            ("Chief Resident: Ana Ruiz", "Ana Ruiz"),
            ("Luis de la Cruz", "Luis de la Cruz"),
            ("Maria Gonzalez, MD Vascular Surgery Fellow", "Maria Gonzalez"),
        ],
    )
    def test_names_are_isolated_from_titles(self, text, expected):
        assert _extract_name(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "Read More About Us", "View All Residents", "Meet Our Team",
            "Department of Surgery", "CURRENT RESIDENTS", "Home",
            # Seen on a live residency page as a sidebar card heading.
            "Alumni Jobs", "Research Resources",
        ],
    )
    def test_navigation_and_headings_are_not_people(self, text):
        assert _extract_name(text) is None

    @pytest.mark.parametrize(
        "text,expected",
        [
            # Live regression: an ASCII-only pattern breaks at the accent and
            # reads this as "Thomas Jr".
            ("Andr\u00e9 Thomas Jr., MD", "Andr\u00e9 Thomas Jr"),
            ("Jos\u00e9 \u00c1lvarez, MD", "Jos\u00e9 \u00c1lvarez"),
            ("Bj\u00f6rn M\u00fcller", "Bj\u00f6rn M\u00fcller"),
            ("Robert Downey III", "Robert Downey III"),
        ],
    )
    def test_accented_names_and_generational_suffixes(self, text, expected):
        assert _extract_name(text) == expected


class TestTableRoster:
    @pytest.fixture(scope="class")
    @staticmethod
    def people():
        return extract_people(
            TABLE_ROSTER,
            page_title="Internal Medicine Residency - Current Residents",
            url="https://x.edu/residents",
        )

    def test_finds_every_resident(self, people):
        assert {"Jane A. Doe", "Robert Chen", "Priya Raman"} <= {
            p.full_name for p in people
        }

    def test_reads_pgy_and_category(self, people):
        by_name = {p.full_name: p for p in people}
        assert by_name["Jane A. Doe"].pgy == 2
        assert by_name["Robert Chen"].pgy == 1
        assert all(
            by_name[n].category is PersonCategory.RESIDENT
            for n in ("Jane A. Doe", "Robert Chen", "Priya Raman")
        )

    def test_decodes_obfuscated_addresses(self, people):
        by_name = {p.full_name: p for p in people}
        assert by_name["Priya Raman"].email == "priya@uchicago.edu"

    def test_program_director_is_collected_as_faculty(self, people):
        # Scope inverted: everyone on the page is kept and labelled.
        grant = next(p for p in people if p.full_name == "Alan Grant")
        assert grant.category is PersonCategory.FACULTY


class TestCardRoster:
    @pytest.fixture(scope="class")
    @staticmethod
    def people():
        return extract_people(
            CARD_ROSTER, page_title="Meet Our Surgery Fellows",
            url="https://x.edu/fellows",
        )

    def test_finds_fellows_with_class_year(self, people):
        by_name = {p.full_name: p for p in people}
        assert by_name["Maria Gonzalez"].class_of == 2027
        assert by_name["Tom O'Brien"].class_of == 2028
        assert all(
            by_name[n].category is PersonCategory.FELLOW
            for n in ("Maria Gonzalez", "Tom O'Brien")
        )

    def test_attending_is_collected_as_faculty(self, people):
        lee = next(p for p in people if p.full_name == "Susan Lee")
        assert lee.category is PersonCategory.FACULTY


class TestEscalation:
    def test_spa_shell_escalates(self):
        should, reason = page_looks_thin(SPA_SHELL, "", 0)
        assert should and "text" in reason

    def test_successful_html_parse_does_not_escalate(self):
        should, _ = page_looks_thin(TABLE_ROSTER, "Jane Doe PGY-2 " * 40, 3)
        assert not should

    def test_roster_without_addresses_escalates(self):
        should, reason = page_looks_thin(
            NO_EMAIL_ROSTER, "Our residents are the heart of the program. " * 30, 0
        )
        assert should and "addresses" in reason


class TestEmailPlausibility:
    @pytest.mark.parametrize("email", ["jane@x.edu", "a.b+c@sub.x.ac.uk"])
    def test_real_addresses_pass(self, email):
        assert is_plausible(email)

    @pytest.mark.parametrize(
        "email", ["logo@2x.png", "a@example.com", "u0040@x.edu", "a@@x.edu"]
    )
    def test_markup_artifacts_rejected(self, email):
        assert not is_plausible(email)

    def test_extracts_and_dedupes(self):
        found = extract_emails("Reach jane@x.edu or JANE@X.EDU, also bob [at] x [dot] edu")
        assert found == ["jane@x.edu", "bob@x.edu"]


class TestLargeRoster:
    """Extraction must be complete and deterministic, not a lucky subset."""

    def test_every_card_is_extracted(self):
        html = large_card_roster(14)
        people = extract_people(
            html, page_title="Current Residents", url="https://x.edu/residents"
        )
        assert len(people) == 14
        assert len({p.email for p in people}) == 14

    def test_repeated_parses_agree(self):
        html = large_card_roster(20)
        counts = {
            len(extract_people(html, page_title="Current Residents", url="https://x.edu/r"))
            for _ in range(5)
        }
        assert counts == {20}


class TestEveryoneIsCollected:
    """Scope inverted: collect everyone published on a page, labelled.

    Previously the extractor discarded faculty, program directors, coordinators,
    students and alumni. Product now wants all of them, with their printed title.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def people():
        return extract_people(
            MIXED_DIRECTORY,
            page_title="Internal Medicine Residency",
            url="https://med.example.edu/team",
        )

    def test_nobody_is_dropped(self, people):
        assert {p.full_name for p in people} == {
            "Jane A. Doe", "Marcus Webb", "Alan Grant", "Ray Arnold",
            "Tim Murphy", "Ellie Sattler",
        }

    @pytest.mark.parametrize(
        "name,category",
        [
            ("Jane A. Doe", PersonCategory.RESIDENT),
            ("Marcus Webb", PersonCategory.RESIDENT),
            ("Alan Grant", PersonCategory.FACULTY),
            ("Ray Arnold", PersonCategory.STAFF),
            ("Tim Murphy", PersonCategory.STUDENT),
            ("Ellie Sattler", PersonCategory.FACULTY),
        ],
    )
    def test_each_person_is_categorised(self, people, name, category):
        person = next(p for p in people if p.full_name == name)
        assert person.category is category

    def test_position_is_kept_verbatim(self, people):
        positions = {p.full_name: p.position for p in people}
        assert positions["Alan Grant"] == "Program Director"
        assert positions["Marcus Webb"] == "Chief Resident"
        assert positions["Ellie Sattler"] == "Associate Professor"

    def test_people_without_an_email_are_kept(self, people):
        webb = next(p for p in people if p.full_name == "Marcus Webb")
        assert webb.email is None
        assert webb.pgy == 3  # name and year are what matter


class TestAlumniAreLabelledNotSkipped:
    def test_alumni_pages_are_recognised(self):
        assert page_is_alumni_listing("Residency Alumni", "https://x.edu/alumni")
        assert not page_is_alumni_listing("Current Residents", "https://x.edu/residents")

    def test_people_on_an_alumni_page_are_collected_as_alumni(self):
        people = extract_people(
            ALUMNI_ROSTER,
            page_title="Residency Alumni",
            url="https://med.example.edu/education/alumni",
        )
        assert [p.full_name for p in people] == ["Gone Person"]
        assert people[0].category is PersonCategory.ALUMNI

    def test_the_page_overrides_the_persons_own_wording(self):
        # Someone described as a resident on an alumni page has already finished.
        assert classify_person("PGY-3 Resident", "", page_is_alumni=True) is (
            PersonCategory.ALUMNI
        )


class TestCategoryPrecedence:
    @pytest.mark.parametrize(
        "text,category",
        [
            # Faculty wins over the word "fellow" inside a job title.
            ("Fellowship Program Director", PersonCategory.FACULTY),
            ("Residency Program Coordinator", PersonCategory.STAFF),
            ("Chief Resident", PersonCategory.RESIDENT),
            ("Clinical Fellow", PersonCategory.FELLOW),
            ("Assistant Professor of Medicine", PersonCategory.FACULTY),
            ("PGY-1", PersonCategory.RESIDENT),
            ("Medical Student", PersonCategory.STUDENT),
            ("", PersonCategory.UNKNOWN),
        ],
    )
    def test_specific_titles_beat_generic_words(self, text, category):
        assert classify_person(text, "") is category


class TestHeadingOnlyDirectory:
    """Faculty pages often publish a bare run of headings.

    No mailto links, no card classes, nothing structural to hook onto. Before
    the heading fallback these pages returned zero people while plainly listing
    dozens, which reads as "nobody works here" rather than as a parse failure.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def people():
        return extract_people(
            HEADING_ONLY_FACULTY,
            page_title="Our Faculty | Department of Radiation Oncology",
            url="https://radonc.example.edu/people/our-faculty",
        )

    def test_every_heading_becomes_a_person(self, people):
        assert {p.full_name for p in people} == {
            "Nishant Agrawal", "Bulent Aydogan", "Stephanie Bennett", "Jason Bugno",
        }

    def test_the_section_heading_is_not_a_person(self, people):
        assert "Our Faculty" not in {p.full_name for p in people}

    def test_titles_come_from_the_text_beside_the_heading(self, people):
        positions = {p.full_name: p.position for p in people}
        assert positions["Nishant Agrawal"] == "Professor"
        assert positions["Stephanie Bennett"] == "Assistant Professor"

    def test_all_are_categorised_as_faculty(self, people):
        assert all(p.category is PersonCategory.FACULTY for p in people)

    def test_credentials_are_stripped_from_names(self, people):
        # PharmD, DVM, JD and friends, not just MD/PhD.
        assert "Jason Bugno" in {p.full_name for p in people}


class TestOrganisationalNames:
    """Directory pages mix people with unit headings.

    Collecting everyone means the extractor is far more permissive than it was,
    so organisational headings have to be rejected explicitly or they arrive as
    people. "BSD Academic Affairs" reached the database during a live run.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "BSD Academic Affairs",
            "Office of Graduate Medical Education",
            "GME Council",
            "Department of Surgery",
            "Clinical Operations Committee",
        ],
    )
    def test_units_are_not_people(self, text):
        assert _extract_name(text) is None

    @pytest.mark.parametrize(
        "text",
        ["Robert Downey III", "Zhen Tian", "Nishant Agrawal", "Andr\u00e9 Thomas Jr"],
    )
    def test_real_names_still_pass(self, text):
        assert _extract_name(text) == text


class TestPrecisionOnRealPages:
    """Defects found by running against live university sites.

    Collecting everyone makes the extractor far more permissive, so these are
    the false positives that showed up in the database and had to be closed.
    """

    @pytest.mark.parametrize(
        "text",
        [
            # Citation lists and call rotas: an initial plus a surname.
            "A Sanchez",
            "B Yu",
            "C Guo",
            # Two people in one cell.
            "A Sanchez, K Little",
        ],
    )
    def test_citation_fragments_are_not_people(self, text):
        assert _extract_name(text) is None

    def test_an_adjacent_email_is_not_absorbed_into_the_name(self):
        assert (
            _extract_name("Adrian Gutierrez adrian.gutierrez@bsd.uchicago.edu")
            == "Adrian Gutierrez"
        )

    @pytest.mark.parametrize(
        "text,expected",
        [("Doe, Jane", "Doe, Jane"), ("Smith, John Paul", "Smith, John Paul")],
    )
    def test_last_comma_first_still_works(self, text, expected):
        assert _extract_name(text) == expected

    def test_a_bare_credential_is_not_a_job_title(self):
        assert _extract_position("Ali Mansour, MD", "Ali Mansour") is None
        assert (
            _extract_position("Jane Doe, MD Program Director", "Jane Doe")
            == "Program Director"
        )

    def test_programme_leadership_pages_are_faculty(self):
        assert classify_person("", "vascular neurology program leadership") is (
            PersonCategory.FACULTY
        )


class TestSectionHeadingsAreNotPeople:
    """The heading fallback reads every <h3>, so page furniture has to be
    rejected explicitly. All of these reached the database on a live run."""

    @pytest.mark.parametrize(
        "text",
        [
            "Clinical Experience",
            "Scholarly Activity",
            "Cardiac Anesthesia Attendings",
            "Resident Wellness",
            "Global Health",
            # A bare specialty is a department heading, never a person.
            "Vascular Surgery",
            "Internal Medicine",
        ],
    )
    def test_page_furniture_is_rejected(self, text):
        assert _extract_name(text) is None

    @pytest.mark.parametrize(
        "text",
        ["Nishant Agrawal", "Maria Gonzalez", "Zhen Tian", "Sean McDonald-Smith"],
    )
    def test_real_names_are_unaffected(self, text):
        assert _extract_name(text) == text


class TestPositionFallbackIsConservative:
    """Profile cards put several labelled fields beside a name.

    The fallback only runs when no recognised title matched, so it has to be
    strict: "MD Medical School: Chicago Med. College" reached the UI as a job
    title before this guard.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "MD Medical School: Chicago Med. College",
            "Medical School: MCW",
            "Hometown: Chicago, IL",
            "Undergraduate: Northwestern",
        ],
    )
    def test_other_fields_are_not_positions(self, text):
        assert _extract_position(text, "Charles Humes") is None

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Program Director", "Program Director"),
            ("Associate Professor", "Associate Professor"),
            ("Chief Resident", "Chief Resident"),
        ],
    )
    def test_recognised_titles_still_work(self, text, expected):
        assert _extract_position(text, "Someone Else") == expected
