"""Import the fixed school catalog without deleting historical crawl data."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from sqlalchemy import select, update

from .db.enums import ValidationStatus
from .db.models import Site
from .urls import canonicalize, host_of

CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "Shaun_23_Medical_Schools_Resident_Links.xlsx"
EXPECTED_SCHOOLS = 23
_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass(frozen=True)
class CatalogSchool:
    name: str
    website: str
    directory_url: str

    @property
    def domain(self) -> str:
        return host_of(self.website)


def _text(cell, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "s":
        return shared[int(cell.findtext("x:v", default="", namespaces=_NS))]
    if kind == "inlineStr":
        return "".join(cell.itertext())
    return cell.findtext("x:v", default="", namespaces=_NS)


def load_catalog(path: Path = CATALOG_PATH) -> list[CatalogSchool]:
    """Read the shipped XLSX with no optional runtime spreadsheet dependency."""
    with ZipFile(path) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall("x:si", _NS)]
        root = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", _NS):
        values = [_text(cell, shared).strip() for cell in row.findall("x:c", _NS)]
        if values:
            rows.append(values)
    header_index = next((i for i, row in enumerate(rows) if row[:3] == ["School Name", "Website", "Directory / Program Index"]), None)
    if header_index is None:
        raise ValueError("catalog workbook must contain School Name, Website, and Directory / Program Index columns")
    schools = [CatalogSchool(*row[:3]) for row in rows[header_index + 1:] if any(row[:3])]
    if len(schools) != EXPECTED_SCHOOLS:
        raise ValueError(f"catalog must contain exactly {EXPECTED_SCHOOLS} schools; found {len(schools)}")
    domains = [school.domain for school in schools]
    if not all(school.name and school.website and school.directory_url and domain for school, domain in zip(schools, domains)):
        raise ValueError("every catalog school needs a name, valid website, and directory URL")
    if len(set(domains)) != len(domains):
        raise ValueError("catalog contains duplicate website domains")
    return schools


def _key(value: str | None) -> str:
    return "".join((value or "").casefold().split())


async def import_catalog(session, *, path: Path = CATALOG_PATH, dry_run: bool = False) -> dict[str, int]:
    """Activate exactly the catalog schools; retain all other rows as history."""
    catalog = load_catalog(path)
    existing = list((await session.execute(select(Site))).scalars())
    matched: set[str] = set()
    created = updated = 0
    for school in catalog:
        website = canonicalize(school.website) or school.website
        directory = canonicalize(school.directory_url) or school.directory_url
        candidates = {
            site.id for site in existing
            if site.root_domain == school.domain
            or (canonicalize(site.canonical_url) or site.canonical_url) == website
            or (site.directory_url and (canonicalize(site.directory_url) or site.directory_url) == directory)
            or _key(site.name or site.hospital_name) == _key(school.name)
        }
        if len(candidates) > 1:
            raise ValueError(f"ambiguous existing school match for {school.name!r}; no changes were made")
        site = next((item for item in existing if item.id in candidates), None)
        if site is None:
            site = Site(root_domain=school.domain, canonical_url=website, name=school.name,
                        hospital_name=school.name, directory_url=directory, is_active=True,
                        validation_status=ValidationStatus.PENDING)
            session.add(site)
            await session.flush()
            existing.append(site)
            created += 1
        else:
            site.canonical_url, site.directory_url, site.name, site.hospital_name, site.is_active = website, directory, school.name, school.name, True
            updated += 1
        matched.add(site.id)
    deactivated = sum(1 for site in existing if site.id not in matched and site.is_active)
    if dry_run:
        await session.rollback()
    else:
        await session.execute(update(Site).where(Site.id.not_in(matched)).values(is_active=False))
        await session.commit()
    return {"active": len(matched), "created": created, "updated": updated, "deactivated": deactivated}
