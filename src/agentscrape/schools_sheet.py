"""The school list comes from spreadsheets checked into `schools/`.

Staff are sent a sheet with one row per institution; it is dropped into the
folder and loaded on startup (or with `agentscrape load-schools`). There is no
upload flow.

Only three columns are guaranteed — institution name, website and people
directory link — and their headers vary from sheet to sheet, so columns are
recognized by loose header matching plus the shape of their values. Everything
else in the sheet is ignored. Loading is an upsert keyed on the crawl entry's
host: it never deletes a school or anything collected for it.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db.enums import ValidationStatus
from .db.models import Site
from .db.repositories.sites import get_site_by_domain
from .urls import canonicalize, entry_url, host_of, registrable_domain

log = logging.getLogger("agentscrape.schools")

SHEET_SUFFIXES = (".csv", ".xlsx")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SchoolRow:
    row: int
    name: str
    website: str
    directory_url: str | None
    # The residency/fellowship hub: a better place to start crawling than the
    # home page, and the sheet's own choice of entry point.
    hub_url: str | None = None

    @property
    def entry_url(self) -> str:
        return self.hub_url or self.website

    @property
    def affiliated_domains(self) -> list[str]:
        """The website's domain, when the hub lives on another one (UChicago:
        gme.uchicago.edu and uchicagomedicine.org), so both are in scope."""
        entry = registrable_domain(host_of(self.entry_url))
        site = registrable_domain(host_of(self.website))
        return [site] if site != entry else []


def sheets_dir() -> Path:
    """The configured folder, else schools/ in the repository or the working
    directory (an installed package has no repository beside it)."""
    if settings.school_sheets_dir:
        return Path(settings.school_sheets_dir)
    for candidate in (Path(__file__).resolve().parents[2] / "schools", Path.cwd() / "schools"):
        if candidate.is_dir():
            return candidate
    return Path.cwd() / "schools"


def _key(header: str) -> str:
    return _NON_ALNUM.sub("", (header or "").lower())


def as_url(value: str | None) -> str | None:
    """A cell as a canonical http(s) URL, or None when it is not one."""
    text = (value or "").strip().strip("\"'")
    if not text or " " in text:
        return None
    if not text.lower().startswith(("http://", "https://")):
        if "." not in text:
            return None
        text = f"https://{text}"
    canonical = canonicalize(text)
    host = host_of(canonical) if canonical else ""
    if not canonical or "." not in host:
        return None
    return canonical


def _column(headers: list[str], rows: list[list[str]], words: tuple[str, ...], *,
            url: bool, exclude: set[int]) -> int | None:
    """The first column whose header contains one of `words` (and, for URL
    columns, whose values are mostly URLs)."""
    for index, header in enumerate(headers):
        if index in exclude or not any(word in _key(header) for word in words):
            continue
        if not url:
            return index
        values = [r[index] for r in rows if index < len(r) and r[index].strip()]
        if values and sum(1 for v in values if as_url(v)) * 2 >= len(values):
            return index
    return None


def parse_school_table(table: list[list[str]]) -> list[SchoolRow]:
    """Rows of a sheet (header first) to schools. Rows missing a required
    value are logged and skipped."""
    table = [[str(c or "").strip() for c in row] for row in table]
    table = [row for row in table if any(row)]
    if not table:
        return []
    headers, body = table[0], table[1:]

    used: set[int] = set()
    # A directory column can legitimately contain values such as "DIRECTORY NOT
    # AVAILABLE". Recognise the column by its header, then treat non-URLs as a
    # missing link for that individual school rather than rejecting the sheet.
    directory = _column(headers, body, ("directory",), url=False, exclude=used)
    if directory is not None:
        used.add(directory)
    hub = _column(headers, body, ("hub", "residency", "fellowship", "gme"), url=True, exclude=used)
    if hub is not None:
        used.add(hub)
    website = _column(headers, body, ("website", "url", "site", "homepage"), url=True, exclude=used)
    # Some source sheets provide only a "Crawler Hub". It is a valid crawl
    # entry, so use it as the website when no separate institutional URL exists.
    if website is None and hub is not None:
        website = hub
    if website is not None:
        used.add(website)
    name = _column(headers, body, ("institution", "school", "name"), url=False, exclude=used)

    missing = [label for label, index in (
        ("institution name", name), ("website", website),
    ) if index is None]
    if missing:
        raise ValueError(f"no column for {', '.join(missing)} in headers {headers}")

    def cell(row: list[str], index: int | None) -> str:
        return row[index] if index is not None and index < len(row) else ""

    out: list[SchoolRow] = []
    for number, row in enumerate(body, start=2):
        school = cell(row, name)
        site = as_url(cell(row, website))
        people = as_url(cell(row, directory))
        if not (school and site):
            log.warning(
                "school sheet row %d skipped: needs a name and website "
                "(got %r, %r, %r)", number, school, cell(row, website), cell(row, directory),
            )
            continue
        out.append(SchoolRow(number, school, site, people, as_url(cell(row, hub))))
    return out


def read_school_sheet(path: Path) -> list[SchoolRow]:
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        table = [list(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]
        return parse_school_table(table)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return parse_school_table(list(csv.reader(io.StringIO(text), dialect)))


@dataclass
class LoadResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_files: int = 0


async def load_school_sheets(session: AsyncSession, folder: Path | None = None) -> LoadResult:
    """Upsert every school in every sheet in `folder`. Idempotent."""
    folder = folder or sheets_dir()
    result = LoadResult()
    paths = sorted(
        p for p in folder.glob("*") if p.suffix.lower() in SHEET_SUFFIXES
    ) if folder.is_dir() else []
    for path in paths:
        try:
            rows = read_school_sheet(path)
        except Exception as exc:
            result.skipped_files += 1
            log.error("school sheet %s could not be read: %s", path.name, exc)
            continue
        for row in rows:
            outcome = await _upsert(session, row)
            setattr(result, outcome, getattr(result, outcome) + 1)
        await session.flush()
    await session.commit()
    if paths:
        log.info(
            "school sheets: %d created, %d updated, %d unchanged (%d files, %d unreadable)",
            result.created, result.updated, result.unchanged, len(paths), result.skipped_files,
        )
    return result


async def _upsert(session: AsyncSession, row: SchoolRow) -> str:
    entry = row.entry_url
    host = host_of(entry)
    canonical = entry_url(entry)
    affiliated = row.affiliated_domains or None
    site = await get_site_by_domain(session, host)
    if site is None:
        session.add(Site(
            root_domain=host, canonical_url=canonical, name=row.name,
            directory_url=row.directory_url, affiliated_domains=affiliated,
            validation_status=ValidationStatus.PENDING,
        ))
        return "created"
    wanted = {
        "name": row.name, "canonical_url": canonical,
        "directory_url": row.directory_url, "affiliated_domains": affiliated,
    }
    if all(getattr(site, attr) == value for attr, value in wanted.items()):
        return "unchanged"
    if site.directory_url != row.directory_url:
        # A different directory has to be learned again.
        site.directory_config = None
    for attr, value in wanted.items():
        setattr(site, attr, value)
    return "updated"
