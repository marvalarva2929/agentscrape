"""Priority-links spreadsheet ingestion: column/worksheet-agnostic URL
extraction, normalization, and deduplication. No LLM involved - deterministic
parsing only."""

from __future__ import annotations

import io

from openpyxl import Workbook

from agentscrape.discovery.priority_urls import (
    extract_urls_from_csv,
    extract_urls_from_xlsx,
    google_sheet_csv_export_url,
)


def _xlsx_bytes(build) -> bytes:
    workbook = Workbook()
    build(workbook)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_urls_found_in_arbitrary_columns_not_just_a_url_column() -> None:
    def build(wb: Workbook) -> None:
        sheet = wb.active
        sheet["A1"] = "Program"
        sheet["B1"] = "Notes"
        sheet["G1"] = "https://www.bcm.edu/im/residency/current-residents"
        sheet["A2"] = "Internal Medicine"
        sheet["GG2"] = "Also see https://www.bcm.edu/im/fellowship for details"

    urls = extract_urls_from_xlsx(_xlsx_bytes(build))
    assert "https://www.bcm.edu/im/residency/current-residents" in urls
    assert any("bcm.edu/im/fellowship" in u for u in urls)


def test_urls_across_multiple_worksheets() -> None:
    def build(wb: Workbook) -> None:
        wb.active.title = "Programs"
        wb.active["A1"] = "https://school.edu/programs/surgery"
        other = wb.create_sheet("Contacts")
        other["C5"] = "https://school.edu/contacts/coordinator"

    urls = extract_urls_from_xlsx(_xlsx_bytes(build))
    assert "https://school.edu/programs/surgery" in urls
    assert "https://school.edu/contacts/coordinator" in urls


def test_hyperlink_target_behind_display_text_is_captured() -> None:
    def build(wb: Workbook) -> None:
        sheet = wb.active
        cell = sheet["A1"]
        cell.value = "Baylor Internal Medicine"
        cell.hyperlink = "https://www.bcm.edu/im/residency/current-residents"

    urls = extract_urls_from_xlsx(_xlsx_bytes(build))
    assert "https://www.bcm.edu/im/residency/current-residents" in urls
    # The display text itself must never be treated as a URL.
    assert "Baylor Internal Medicine" not in urls


def test_unrelated_spreadsheet_data_is_ignored() -> None:
    def build(wb: Workbook) -> None:
        sheet = wb.active
        sheet["A1"] = "Program"
        sheet["B1"] = "Specialty"
        sheet["C1"] = "Contact"
        sheet["A2"] = "Internal Medicine"
        sheet["B2"] = "Cardiology"
        sheet["C2"] = "jane.doe@example.edu"
        sheet["D2"] = 42

    urls = extract_urls_from_xlsx(_xlsx_bytes(build))
    assert urls == []


def test_duplicate_urls_are_normalized_and_deduplicated() -> None:
    def build(wb: Workbook) -> None:
        sheet = wb.active
        sheet["A1"] = "https://www.BCM.edu/im/residency/"
        sheet["A2"] = "https://www.bcm.edu/im/residency"
        sheet["A3"] = "https://www.bcm.edu/im/residency/"

    urls = extract_urls_from_xlsx(_xlsx_bytes(build))
    assert len(urls) == 1


def test_csv_extraction_scans_every_field_regardless_of_header() -> None:
    content = (
        b"name,notes,link\n"
        b"Jane Roe,see program page,https://school.edu/residents/jane\n"
        b"no header here,,\n"
        b"extra,https://school.edu/fellows,\n"
    )
    urls = extract_urls_from_csv(content)
    assert "https://school.edu/residents/jane" in urls
    assert "https://school.edu/fellows" in urls


def test_csv_ignores_non_url_fields() -> None:
    content = b"a,b,c\n1,two,three\n"
    assert extract_urls_from_csv(content) == []


def test_google_sheet_export_url_extracts_id_and_gid() -> None:
    url = google_sheet_csv_export_url(
        "https://docs.google.com/spreadsheets/d/ABC123XYZ/edit#gid=456"
    )
    assert url == "https://docs.google.com/spreadsheets/d/ABC123XYZ/export?format=csv&gid=456"


def test_google_sheet_export_url_defaults_gid_to_zero() -> None:
    url = google_sheet_csv_export_url("https://docs.google.com/spreadsheets/d/ABC123XYZ/edit")
    assert url == "https://docs.google.com/spreadsheets/d/ABC123XYZ/export?format=csv&gid=0"


def test_non_sheet_url_is_rejected() -> None:
    assert google_sheet_csv_export_url("https://example.com/not-a-sheet") is None
