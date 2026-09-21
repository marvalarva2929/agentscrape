"""The client's fixed list of schools.

`data/schools-23.csv` is the list of institutions we are assigned to crawl. It
is imported on startup and makes exactly those schools active: they are the ones
the school picker offers. Every other school row stays in the database with its
people and history, inactive, so nothing collected is ever deleted.

One row per school:

* `entry_url` — where a crawl starts: the graduate-medical-education hub, which
  lists the programs. A hub, not a home page, because a patient-facing home page
  often exposes no path to the programs at all.
* `homepage` — the institution's own site, kept for reference and as the place
  to fall back to.
* `directory_url` — the people directory, blank when the school has none.
* `program_index_url` — a second page listing programs, used by the preflight.
* `affiliated_domains` — other sites the school publishes rosters on, separated
  by `;`. A crawl only follows links within the entry's own site and these.

A school is identified by the host of its `entry_url`, so re-importing updates
the row and its history in place. A row that cannot be saved is logged and
skipped; it never stops the others or the API from starting.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db.enums import ValidationStatus
from .db.models import Site
from .urls import canonicalize, entry_url, host_of, registrable_domain

log = logging.getLogger("agentscrape.catalog")

CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "schools-23.csv"
EXPECTED_SCHOOLS = 23


@dataclass(frozen=True)
class CatalogSchool:
    name: str
    entry_url: str
    homepage: str = ""
    directory_url: str = ""
    program_index_url: str = ""
    affiliated_domains: tuple[str, ...] = field(default_factory=tuple)
    tier: str = ""

    @property
    def host(self) -> str:
        return host_of(self.entry_url)

    @property
    def scope_domains(self) -> list[str]:
        """Other registrable domains a crawl of this school may follow into."""
        own = registrable_domain(self.host)
        extra = list(self.affiliated_domains)
        # The homepage's domain, when the hub lives on another one.
        if self.homepage:
            extra.append(registrable_domain(host_of(self.homepage)))
        return list(dict.fromkeys(d for d in extra if d and d != own))


def _clean_url(value: str) -> str:
    value = (value or "").strip()
    if not value.lower().startswith(("http://", "https://")):
        return ""
    return canonicalize(value) or value


def load_catalog(path: Path = CATALOG_PATH, *, expected: int | None = EXPECTED_SCHOOLS) -> list[CatalogSchool]:
    """Read and validate the catalog. Raises ValueError if it is not usable."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    schools = [
        CatalogSchool(
            name=(row.get("name") or "").strip(),
            entry_url=_clean_url(row.get("entry_url") or ""),
            homepage=_clean_url(row.get("homepage") or ""),
            directory_url=_clean_url(row.get("directory_url") or ""),
            program_index_url=_clean_url(row.get("program_index_url") or ""),
            affiliated_domains=tuple(
                d.strip().lower() for d in (row.get("affiliated_domains") or "").split(";") if d.strip()
            ),
            tier=(row.get("tier") or "").strip(),
        )
        for row in rows
        if any((value or "").strip() for value in row.values())
    ]
    problems = [f"row {n}: {s.name or '(no name)'}" for n, s in enumerate(schools, start=1)
                if not (s.name and s.entry_url and s.host)]
    if problems:
        raise ValueError("catalog rows need a name and a valid entry_url: " + "; ".join(problems))
    if expected is not None and len(schools) != expected:
        raise ValueError(f"catalog must list exactly {expected} schools; found {len(schools)}")
    hosts = [s.host for s in schools]
    if len(set(hosts)) != len(hosts):
        dupes = sorted({h for h in hosts if hosts.count(h) > 1})
        raise ValueError(f"catalog lists the same entry host twice: {', '.join(dupes)}")
    return schools


def _key(value: str | None) -> str:
    return "".join((value or "").casefold().split())


def _match(existing: list[Site], school: CatalogSchool) -> Site | None:
    """The row this school already has: by entry host, else by name when the
    entry moved to another host (the row keeps its history either way)."""
    for site in existing:
        if site.root_domain == school.host:
            return site
    by_name = [s for s in existing if _key(s.name or s.hospital_name) == _key(school.name)]
    return by_name[0] if len(by_name) == 1 else None


async def import_catalog(
    session: AsyncSession, *, path: Path = CATALOG_PATH, dry_run: bool = False
) -> dict[str, int]:
    """Make exactly the catalog's schools active. Idempotent; never deletes."""
    catalog = load_catalog(path)
    existing = list((await session.execute(select(Site))).scalars())
    matched: set[str] = set()
    created = updated = failed = 0

    for school in catalog:
        canonical = entry_url(school.entry_url)
        try:
            async with session.begin_nested():
                site = _match(existing, school)
                if site is None:
                    site = Site(
                        root_domain=school.host, canonical_url=canonical, name=school.name,
                        hospital_name=school.name, directory_url=school.directory_url or None,
                        affiliated_domains=school.scope_domains or None, is_active=True,
                        validation_status=ValidationStatus.PENDING,
                    )
                    session.add(site)
                    await session.flush()
                    existing.append(site)
                    created += 1
                else:
                    site.root_domain = school.host
                    site.canonical_url = canonical
                    site.name = school.name
                    site.hospital_name = school.name
                    site.affiliated_domains = school.scope_domains or None
                    # A catalog row without a directory never erases one already known.
                    directory = school.directory_url or site.directory_url
                    if directory != site.directory_url:
                        site.directory_config = None  # a new directory has to be learned again
                    site.directory_url = directory
                    site.is_active = True
                    await session.flush()
                    updated += 1
                matched.add(site.id)
        except Exception as exc:
            failed += 1
            log.error("catalog school %r could not be saved: %s", school.name, exc)

    deactivated = sum(1 for s in existing if s.id not in matched and s.is_active)
    if dry_run:
        await session.rollback()
    else:
        # Only when every school was placed: a partial import must not hide the
        # schools it failed to save.
        if not failed and matched:
            await session.execute(
                update(Site).where(Site.id.not_in(matched)).values(is_active=False)
            )
        await session.commit()
    return {
        "active": len(matched), "created": created, "updated": updated,
        "failed": failed, "deactivated": 0 if failed else deactivated,
    }
