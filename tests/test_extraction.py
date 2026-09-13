"""HTML extraction and the decision to escalate to a browser."""

from __future__ import annotations

import pytest

from agentscrape.db.enums import RecordRole
from agentscrape.extraction.html_people import (
    _extract_name,
    extract_people,
    page_is_out_of_scope,
    page_looks_thin,
)
from agentscrape.validation.email import extract_emails, is_plausible

from .fixtures import (
    CARD_ROSTER,
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
        assert {p.full_name for p in people} == {
            "Jane A. Doe", "Robert Chen", "Priya Raman"
        }

    def test_reads_pgy_and_role(self, people):
        by_name = {p.full_name: p for p in people}
        assert by_name["Jane A. Doe"].pgy == 2
        assert by_name["Robert Chen"].pgy == 1
        assert all(p.role is RecordRole.RESIDENT for p in people)

    def test_decodes_obfuscated_addresses(self, people):
        by_name = {p.full_name: p for p in people}
        assert by_name["Priya Raman"].email == "priya@uchicago.edu"

    def test_program_director_is_out_of_scope(self, people):
        assert "Alan Grant" not in {p.full_name for p in people}


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
        assert all(p.role is RecordRole.FELLOW for p in people)

    def test_attending_is_out_of_scope(self, people):
        assert "Susan Lee" not in {p.full_name for p in people}


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


class TestOutOfScopePages:
    """Alumni listings are former trainees; the client wants current ones."""

    @pytest.mark.parametrize(
        "title,url",
        [
            ("Alumni | Department of Radiation and Cellular Oncology",
             "https://radonc.example.edu/education/residency-alumni-job-placement-list"),
            ("Former Residents", "https://x.edu/people"),
            ("Our Graduates", "https://x.edu/education/graduates"),
        ],
    )
    def test_past_trainee_pages_are_refused(self, title, url):
        assert page_is_out_of_scope(title, url) is not None
        assert extract_people(TABLE_ROSTER, page_title=title, url=url) == []

    @pytest.mark.parametrize(
        "title,url",
        [("Current Residents", "https://x.edu/people/current-residents"),
         ("Meet Our Fellows", "https://x.edu/fellows")],
    )
    def test_current_rosters_are_allowed(self, title, url):
        assert page_is_out_of_scope(title, url) is None
        assert extract_people(TABLE_ROSTER, page_title=title, url=url)


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
