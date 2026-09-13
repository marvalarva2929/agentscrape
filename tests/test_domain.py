"""Domain primitives: identity, PGY/class-of, specialty, URLs.

These encode decisions that are expensive to reverse (the record identity key in
particular), so the cases below are the contract, not incidental coverage.
"""

from __future__ import annotations

from datetime import date

import pytest

from agentscrape.api.errors import AppError
from agentscrape.api.pagination import Cursor
from agentscrape.domain.matching import (
    IdentityKind,
    build_identity,
    diff_fields,
    is_role_account,
    normalize_email,
    normalize_name,
)
from agentscrape.domain.pgy import (
    academic_year,
    class_of_from_pgy,
    current_pgy,
    parse_class_of,
    parse_pgy,
    pgy_from_class_of,
    resolve_year_fields,
)
from agentscrape.domain.specialty import (
    final_pgy,
    infer_specialty,
    normalize_specialty,
    program_years,
)
from agentscrape.urls import canonicalize, registrable_domain, url_hash


class TestEmailNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Jane.Doe@UChicago.edu", "jane.doe@uchicago.edu"),
            ("  jane@x.edu  ", "jane@x.edu"),
            ("mailto:a.b@x.edu?subject=hi", "a.b@x.edu"),
            ("<jane@x.edu>", "jane@x.edu"),
            ("not-an-email", None),
            (None, None),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_email(raw) == expected

    def test_plus_and_dots_are_preserved(self):
        # On .edu these are distinct mailboxes, so folding them would merge people.
        assert normalize_email("a.b+tag@x.edu") == "a.b+tag@x.edu"


class TestRoleAccounts:
    @pytest.mark.parametrize(
        "email",
        ["info@x.edu", "residency@x.edu", "gme@x.edu", "peds-residency@x.edu",
         "im.program@x.edu", "contact@x.edu", "no-reply@x.edu"],
    )
    def test_office_addresses_flagged(self, email):
        assert is_role_account(email)

    @pytest.mark.parametrize("email", ["jane.doe@x.edu", "jsmith@x.edu", "tobrien@x.edu"])
    def test_personal_addresses_not_flagged(self, email):
        assert not is_role_account(email)


class TestNameNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Dr. Jane A. Doe, MD, MPH", "jane doe"),
            ("Doe, Jane", "jane doe"),
            ("JANE DOE", "jane doe"),
            ("José Álvarez, M.D.", "jose alvarez"),
            ("Tom O'Brien", "tom o'brien"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_name(raw) == expected

    def test_middle_initial_dropped_so_variants_match(self):
        assert normalize_name("Jane A. Doe") == normalize_name("Jane Doe")


class TestIdentity:
    def test_email_is_the_key(self):
        identity = build_identity(email="Jane.Doe@x.edu", full_name="Dr. Jane Doe MD")
        assert identity.key == "email:jane.doe@x.edu"
        assert identity.kind is IdentityKind.EMAIL
        assert not identity.role_account

    def test_shared_office_inbox_falls_back_to_name(self):
        # Without this guard, everyone listed under info@ collapses into one record.
        a = build_identity(email="info@x.edu", full_name="Jane Doe", role="resident")
        b = build_identity(email="info@x.edu", full_name="John Roe", role="resident")
        assert a.key != b.key
        assert a.kind is IdentityKind.NAME
        assert a.role_account

    def test_same_email_different_name_is_one_record(self):
        a = build_identity(email="jane@x.edu", full_name="Jane Doe")
        b = build_identity(email="jane@x.edu", full_name="Jane Smith")
        assert a.key == b.key  # a rename, recorded as a field diff

    def test_no_email_and_no_name_is_unstorable(self):
        assert build_identity(email=None, full_name=None) is None


class TestDiff:
    def test_change_is_recorded_with_previous_value(self):
        changes = diff_fields({"full_name": "jane doe"}, {"full_name": "jane smith"})
        assert changes == {"full_name": {"from": "jane doe", "to": "jane smith"}}

    def test_incoming_null_does_not_erase_known_data(self):
        # A page that stopped listing a PGY is missing info, not a cleared field.
        assert diff_fields({"class_of": 2027}, {"class_of": None}) == {}

    def test_identical_is_no_change(self):
        assert diff_fields({"email": "a@x.edu"}, {"email": "a@x.edu"}) == {}


class TestAcademicYear:
    def test_july_starts_the_new_year(self):
        assert academic_year(date(2026, 6, 30)) == 2025
        assert academic_year(date(2026, 7, 1)) == 2026


class TestPgy:
    @pytest.mark.parametrize(
        "text,expected",
        [("PGY-2", 2), ("pgy3", 3), ("Post-Graduate Year Three", 3), ("R2", 2),
         ("Chief Resident", None), (None, None)],
    )
    def test_parse(self, text, expected):
        assert parse_pgy(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [("Class of 2027", 2027), ("Class of '28", 2028), ("Graduating 2026", 2026),
         ("Residents", None)],
    )
    def test_parse_class_of(self, text, expected):
        assert parse_class_of(text) == expected

    def test_rolls_forward_across_july(self):
        captured = date(2025, 9, 1)
        assert current_pgy(2, captured, today=date(2026, 6, 30)) == 2   # same AY
        assert current_pgy(2, captured, today=date(2026, 9, 11)) == 3   # next AY

    def test_stale_capture_returns_none_rather_than_nonsense(self):
        assert current_pgy(3, date(2015, 9, 1), today=date(2026, 9, 11)) is None

    def test_class_of_and_pgy_round_trip(self):
        captured = date(2025, 9, 1)
        class_of = class_of_from_pgy(1, captured, final_pgy=3)
        assert class_of == 2028
        assert pgy_from_class_of(class_of, captured, final_pgy=3) == 1

    def test_final_pgy_semantics_not_programme_length(self):
        # Radiation Oncology is a four-year programme whose residents are PGY-2
        # through PGY-5. Using the accredited length here yields no class year
        # for a PGY-5, which is exactly the graduating cohort.
        assert final_pgy("Radiation Oncology") == 5
        assert class_of_from_pgy(5, date(2026, 9, 1), final_pgy=5) == 2027

    def test_backfill_marks_derived_values(self):
        fields = resolve_year_fields(
            pgy_at_capture=2, class_of=None, capture_date=date(2025, 9, 1),
            program_years=5,
        )
        assert fields.pgy_source == "extracted"
        assert fields.class_of == 2029
        assert fields.class_of_source == "derived"

    def test_no_backfill_without_a_known_program_length(self):
        fields = resolve_year_fields(
            pgy_at_capture=2, class_of=None, capture_date=date(2025, 9, 1),
            program_years=None,
        )
        assert fields.class_of is None and fields.class_of_source is None


class TestSpecialty:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Department of Internal Medicine Residency Program", "Internal Medicine"),
            ("IM", "Internal Medicine"),
            ("Dept. of Medicine", "Internal Medicine"),
            ("ob/gyn", "Obstetrics and Gynecology"),
            ("Otolaryngology - Head and Neck Surgery", "Otolaryngology"),
            ("Heme/Onc", "Hematology and Oncology"),
        ],
    )
    def test_collapses_to_vocabulary(self, raw, expected):
        assert normalize_specialty(raw).canonical == expected

    def test_non_specialty_text_is_not_guessed(self):
        assert normalize_specialty("Our Residents").canonical is None

    def test_url_path_and_subdomain_are_used(self):
        assert infer_specialty(url="https://surgery.x.edu/residency").canonical == (
            "General Surgery"
        )
        assert infer_specialty(
            page_title="Meet Our Residents", url="https://x.edu/pediatrics/residents"
        ).canonical == "Pediatrics"

    def test_program_length_known_for_backfill(self):
        assert program_years("Internal Medicine") == 3
        assert program_years(None) is None


class TestUrls:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://Medicine.UChicago.edu/residents/", "https://medicine.uchicago.edu/residents"),
            ("http://x.edu/a/index.html?utm_source=q&b=2#top", "http://x.edu/a?b=2"),
            ("mailto:a@b.c", None),
        ],
    )
    def test_canonicalize(self, raw, expected):
        assert canonicalize(raw) == expected

    def test_variants_share_a_hash_so_the_visited_set_works(self):
        assert url_hash("https://x.edu/a/") == url_hash("https://x.edu/a?utm_source=q")

    @pytest.mark.parametrize(
        "host,expected",
        [("surgery.medicine.uchicago.edu", "uchicago.edu"), ("x.ac.uk", "x.ac.uk"),
         ("a.b.k12.ca.us", "b.k12.ca.us")],
    )
    def test_registrable_domain(self, host, expected):
        assert registrable_domain(host) == expected


class TestCursor:
    def test_round_trip(self):
        cursor = Cursor("2026-01-01T00:00:00Z", "rec_abc")
        assert Cursor.decode(cursor.encode()) == cursor

    def test_malformed_cursor_is_a_clean_error(self):
        with pytest.raises(AppError):
            Cursor.decode("!!!not-a-cursor!!!")

    def test_none_means_first_page(self):
        assert Cursor.decode(None) is None
