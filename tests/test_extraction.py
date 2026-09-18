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
            # The last two to survive a live crawl.
            "Appreciation Award",
            "Call Responsibilities",
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


class TestNamesPrintedWithTheirRole:
    """Real rosters print the role next to the name, in the same element.

    Each of these is a string taken from the Arizona scrape, where the printed
    role or an image caption ended up stored as part of the person's name.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            # A trailing role after a comma. "resident/fellow" is not a stopword
            # while "resident" is, so the name was accepted whole until the
            # splitter learned about "/".
            ("Abbey Bayless,  Resident/Fellow", "Abbey Bayless"),
            ("Sara Fallahi ,  Resident/Fellow", "Sara Fallahi"),
            ("Arunbalaji Pugazhendhi ,  Resident", "Arunbalaji Pugazhendhi"),
            ("Ana Ruiz | Program Coordinator", "Ana Ruiz"),
            # A credential this pattern did not cover.
            ("Lee McGhan , MBBCh", "Lee McGhan"),
        ],
    )
    def test_printed_role_is_not_part_of_the_name(self, text, expected):
        assert _extract_name(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            # A quoted nickname splits the name into two runs; the longest run
            # then absorbed the image caption beside it instead of the surname.
            ("Image Nicholas “Nick” D’Amico", "Nicholas D’Amico"),
            ("Image Marcelina “Marcy” Belmont", "Marcelina Belmont"),
            ("Laura “Lo” Cantu", "Laura Cantu"),
            ("Robert 'Bob' Chen", "Robert Chen"),
        ],
    )
    def test_nickname_and_caption_do_not_displace_the_surname(self, text, expected):
        assert _extract_name(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["Tom O’Brien", "Mary O'Neill", "Sean McDonald-Smith", "Doe, Jane"],
    )
    def test_apostrophes_and_inverted_names_still_survive(self, text):
        assert _extract_name(text) == text


class TestFellowsAreNotLabelledResidents:
    """A fellowship roster prints a PGY number too.

    Testing the PGY before the word "fellow" labelled every cardiology fellow a
    resident, which is the R/F column the client sorts their outreach by.
    """

    @pytest.mark.parametrize(
        "text,category",
        [
            ("Fellow PGY-6", PersonCategory.FELLOW),
            ("Cardiology Fellow, PGY-5", PersonCategory.FELLOW),
            ("PGY-2 Resident", PersonCategory.RESIDENT),
            ("PGY-4", PersonCategory.RESIDENT),
            # Faculty is still tested first, so a director stays faculty.
            ("Fellowship Program Director, PGY-7", PersonCategory.FACULTY),
        ],
    )
    def test_printed_title_beats_the_pgy_number(self, text, category):
        assert classify_person(text, "") is category

    def test_a_resident_planning_a_fellowship_is_still_a_resident(self):
        assert (
            classify_person("PGY-3 Resident, applying to a GI fellowship", "")
            is PersonCategory.RESIDENT
        )


LAYOUT_TABLE_BIOS = """
<html><head><title>Psychiatry Fellowship Programs</title></head><body>
<h2>Addiction Psychiatry Fellows</h2>
<table>
  <tr>
    <td>
      <p><strong>Michael Sheehy, DO</strong></p>
      <p>As a practicing emergency physician for the last 20-plus years I have
      seen the daily effect substance use disorders have on my patients, their
      families and my ED staff. During my emergency medicine career I came to
      understand that the tools I had were not enough, and I want to be at the
      forefront of a cultural shift toward compassionate, trauma-informed care
      for people living with addiction in our community and across the state.</p>
    </td>
    <td>
      <p><strong>Priya Raman, MD</strong></p>
      <p>I grew up in Tucson and returned for residency because the patients
      here are the patients I wanted to serve. My interest in the intersection
      of chronic pain and substance use began on an inpatient consult rotation
      and has shaped every rotation since.</p>
    </td>
  </tr>
</table>
</body></html>
"""


class TestLayoutTableBios:
    """A table with no header row, one person per cell, prose under the name.

    Departments publish fellowship bios this way. There is no header to drive
    column parsing, no card class to hook onto and no mailto link, and the cell
    is far longer than a card block — so every shape handler passed it over and
    the people on it were silently lost.
    """

    def test_people_in_headerless_table_cells_are_found(self):
        names = {p.full_name for p in extract_people(LAYOUT_TABLE_BIOS, url="/x")}
        assert names == {"Michael Sheehy", "Priya Raman"}

    def test_a_long_bio_does_not_become_a_job_title(self):
        for person in extract_people(LAYOUT_TABLE_BIOS, url="/x"):
            assert person.position is None or len(person.position) <= 90

    def test_a_real_data_table_still_yields_nobody(self):
        """The cell scan must not turn a table of numbers into people."""
        data = """
        <table>
          <tr><td>Rotation</td><td>Weeks</td></tr>
          <tr><td>Inpatient Consult</td><td>12</td></tr>
          <tr><td>Community Clinic</td><td>8</td></tr>
        </table>
        """
        assert extract_people(data, url="/x") == []


ROSTER_WITH_ALUMNI_BLOCK = """
<html><head><title>Internal Medicine: Current and Past Residents</title></head><body>
<h1>Internal Medicine: Current and Past Residents</h1>

<h1>Chief Residents</h1>
<div class="card"><h3>Amrutha Doniparthi, MD</h3><p>Resident/Fellow</p>
  <a href="mailto:doniparthi@example.edu">doniparthi@example.edu</a></div>

<h1>PGY-3</h1>
<div class="card"><h3>Monica Angeletti, MD</h3><p>Resident/Fellow</p>
  <a href="mailto:angeletti@example.edu">angeletti@example.edu</a></div>

<h1>Alumni</h1>
<h2>Class of 2025</h2>
<table>
  <tr><th>Name</th><th>Prior Education</th><th>Next Stop</th></tr>
  <tr><td>Audrey Adkins, MD</td><td>University of Arizona, 2022</td><td>Hospitalist, Phoenix</td></tr>
</table>
<h2>Class of 2024</h2>
<table>
  <tr><th>Name</th><th>Medical School</th><th>Next Stop</th></tr>
  <tr><td>Kalkidan Abebe, MD</td><td>Ross University, 2020</td><td>Outpatient Medicine</td></tr>
</table>
</body></html>
"""


class TestAlumniBlocksOnCurrentRosters:
    """`.../current-and-past-residents` is the commonest roster URL on a .edu
    medical site, and it carries both groups on one page.

    The split is stated only in the page's own headings. Judging the page by its
    title labelled every graduate a current resident: across Arizona's internal
    medicine, paediatrics and anaesthesiology rosters that was 407 of 658 people,
    and it is what pushed the site's resident count past its true roster.
    """

    def _by_name(self):
        return {
            p.full_name: p
            for p in extract_people(
                ROSTER_WITH_ALUMNI_BLOCK,
                page_title="Internal Medicine: Current and Past Residents",
                url="https://x.edu/im/current-and-past-residents",
            )
        }

    def test_people_above_the_alumni_heading_are_current(self):
        found = self._by_name()
        assert found["Amrutha Doniparthi"].category is PersonCategory.RESIDENT
        assert found["Monica Angeletti"].category is PersonCategory.RESIDENT

    def test_people_below_the_alumni_heading_are_alumni(self):
        found = self._by_name()
        assert found["Audrey Adkins"].category is PersonCategory.ALUMNI
        assert found["Kalkidan Abebe"].category is PersonCategory.ALUMNI

    def test_a_per_year_subheading_does_not_hide_the_alumni_heading(self):
        """The heading directly above a graduate reads "Class of 2025"; the one
        that says they have left is the <h1> above every year."""
        assert self._by_name()["Audrey Adkins"].category is PersonCategory.ALUMNI

    def test_everyone_on_the_page_is_still_collected(self):
        assert len(self._by_name()) == 4


class TestCombinedResidentFellowTerm:
    """Drupal-built medical schools print one taxonomy term, "Resident/Fellow",
    on the card of every trainee whatever they actually are.

    It names both roles, so it is not a claim about either. Reading it as a claim
    about fellows relabelled a whole anaesthesiology residency; reading it as a
    claim about residents would relabel a cardiology fellowship.
    """

    @pytest.mark.parametrize(
        "page_context,expected",
        [
            # The roster says which programme it is.
            ("Anesthesiology Current and Past Residents", PersonCategory.RESIDENT),
            ("Cardiology Fellows | Sarver Heart Center", PersonCategory.FELLOW),
            ("PGY-2", PersonCategory.RESIDENT),
            # A directory that names no programme cannot say, so neither do we.
            ("Our Team Leadership | College of Medicine", PersonCategory.UNKNOWN),
            ("", PersonCategory.UNKNOWN),
        ],
    )
    def test_the_roster_disambiguates_the_combined_term(self, page_context, expected):
        assert classify_person("Resident/Fellow", page_context) is expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            # One role noun is a real claim and settles it on its own.
            ("Resident", PersonCategory.RESIDENT),
            ("Chief Resident", PersonCategory.RESIDENT),
            ("Clinical Fellow", PersonCategory.FELLOW),
            ("Fellow PGY-6", PersonCategory.FELLOW),
            # A bare PGY is a training year; nothing else carries one.
            ("PGY-2", PersonCategory.RESIDENT),
        ],
    )
    def test_a_single_role_noun_does_not_need_the_page(self, text, expected):
        assert classify_person(text, "") is expected

    def test_a_pgy_on_a_fellowship_roster_is_a_fellow(self):
        assert (
            classify_person("PGY-6", "Cardiology Fellowship Program Fellows")
            is PersonCategory.FELLOW
        )


DIRECTORY_TABLE_WITH_NESTED_TITLES = """
<table>
  <tr><th>Name</th><th>Department</th><th>Email</th><th>Phone</th></tr>
  <tr><td>Obaidah Adi<div class="name-title">Resident Instructor - 3rd Yr</div></td>
      <td>Internal Med Dept Ama Genl</td>
      <td><a href="mailto:obaidah.adi@ttuhsc.edu">obaidah.adi@ttuhsc.edu</a></td>
      <td>(806) 414-9100</td></tr>
  <tr><td>Celine Zhong<div class="name-title">Recurrent Faculty Member</div></td>
      <td>Pharmacy Practice Dal</td>
      <td><a href="mailto:cezhong@ttuhsc.edu">cezhong@ttuhsc.edu</a></td>
      <td>(325) 696-0501</td></tr>
</table>
"""


class TestTableCellsWithNestedTitles:
    """An institution-wide directory prints the title inside the name cell.

    Joining a cell's text without a separator produced "Obaidah AdiResident
    Instructor - 3rd Yr": the role noun then has no word boundary in front of it,
    so 135 Texas Tech residents the client's sheet had confirmed were classified
    `unknown`. Reading the cell's own text separately keeps the title out of the
    surname as well.
    """

    def _by_name(self):
        return {
            p.full_name: p
            for p in extract_people(
                DIRECTORY_TABLE_WITH_NESTED_TITLES, page_title="Directory", url="/dir"
            )
        }

    def test_the_nested_title_is_read_as_a_role(self):
        assert self._by_name()["Obaidah Adi"].category is PersonCategory.RESIDENT

    def test_the_nested_title_does_not_become_part_of_the_name(self):
        found = self._by_name()
        assert "Celine Zhong" in found
        assert "Celine Zhong Recurrent" not in found

    def test_a_faculty_member_is_not_a_trainee(self):
        assert self._by_name()["Celine Zhong"].category is PersonCategory.FACULTY

    def test_the_address_still_comes_from_its_own_cell(self):
        assert self._by_name()["Obaidah Adi"].email == "obaidah.adi@ttuhsc.edu"


class TestAnaesthesiologyTrainingYears:
    """CA-1..CA-3 is the anaesthesiology equivalent of PGY-n.

    It is what the client's own sheet records in its PGY column, and UChicago
    heads each section of its roster with it. Without it every one of those 141
    people came back `unknown`, because the page is titled "Residents & Fellows"
    and that names both roles.
    """

    @pytest.mark.parametrize("section", ["CA-1", "CA-2", "CA 3", "PGY-2"])
    def test_a_training_year_section_means_resident(self, section):
        assert (
            classify_person("Brandon Alford, MD", "Residents & Fellows", section=section)
            is PersonCategory.RESIDENT
        )

    def test_a_fellows_section_on_the_same_page_means_fellow(self):
        assert (
            classify_person("Serene Hoskins, MD", "Residents & Fellows", section="Fellows")
            is PersonCategory.FELLOW
        )

    def test_the_section_is_asked_before_the_page(self):
        """"Residents & Fellows" names both, so the page cannot settle it."""
        assert (
            classify_person("Someone Here", "Residents & Fellows") is PersonCategory.UNKNOWN
        )


class TestEducationHistoryIsNotACurrentRole:
    """A profile card introduces where someone has been with a labelled field.

    "Undergraduate: University of Florida" on a PGY-1's card matched the student
    pattern and turned 35 Chicago pathology residents into students. The label is
    what misfires, so only the label is stripped; the value after it is harmless.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            (
                "Resident (AP/CP) Medical School: Lincoln Memorial "
                "Undergraduate: University of Florida",
                PersonCategory.RESIDENT,
            ),
            # The same trap in the other direction.
            ("PGY-2 Resident Fellowship: Mayo Clinic", PersonCategory.RESIDENT),
            ("Clinical Fellow Residency: Johns Hopkins", PersonCategory.FELLOW),
        ],
    )
    def test_a_history_field_does_not_decide_the_role(self, text, expected):
        assert classify_person(text, "") is expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            # A real student label is still a student.
            ("Medical Student", PersonCategory.STUDENT),
            ("Graduate student in immunology", PersonCategory.STUDENT),
            ("MS3", PersonCategory.STUDENT),
            ("Undergraduate research assistant", PersonCategory.STUDENT),
        ],
    )
    def test_real_student_titles_still_classify(self, text, expected):
        assert classify_person(text, "") is expected
