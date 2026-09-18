"""Move one school's crawl results between databases.

A demo or a fresh deployment should open on real results without crawling
first: a full crawl takes hours and costs model spend. `export_school` writes a
school's people with their provenance to JSON; `import_school` replays them
through the normal reconciliation path, so the imported records, versions and
programmes are exactly what a crawl would have produced. Screenshots are not
carried over; provenance (URL, page title, capture time) is.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from .db.enums import (
    ExtractionMethod,
    FetchMode,
    PersonCategory,
    RunStatus,
    SiteRunStatus,
    ValidationStatus,
)
from .db.models import Record, RecordVersion, Run, Site, SiteRun
from .db.repositories.programs import refresh_program_counts
from .db.repositories.records import ExtractionContext, lock_site, reconcile_people
from .db.session import session_scope
from .extraction.person import ExtractedPerson

SNAPSHOT_VERSION = 1


async def export_school(root_domain: str, path: Path) -> int:
    async with session_scope() as session:
        site = await session.scalar(select(Site).where(Site.root_domain == root_domain))
        if site is None:
            raise SystemExit(f"no school in the database for {root_domain!r}")
        rows = (
            await session.execute(
                select(Record, RecordVersion)
                .join(RecordVersion, RecordVersion.id == Record.current_version_id)
                .where(Record.site_id == site.id)
                .order_by(RecordVersion.source_url, Record.full_name)
            )
        ).all()
        people = [
            {
                "full_name": record.full_name,
                "email": record.email,
                "category": record.category,
                "position": record.position,
                "pgy": record.pgy_at_capture,
                "class_of": record.class_of,
                "specialty": record.specialty_raw,
                "confidence": record.confidence,
                "source_url": version.source_url,
                "page_title": version.page_title,
                "extraction_method": version.extraction_method,
                "fetch_mode": version.fetch_mode,
                "captured_at": version.captured_at.isoformat(),
            }
            for record, version in rows
        ]
        payload = {
            "version": SNAPSHOT_VERSION,
            "school": {
                "root_domain": site.root_domain,
                "canonical_url": site.canonical_url,
                "name": site.name,
                "hospital_name": site.hospital_name,
                "location": site.location,
                "institution_type": site.institution_type,
            },
            "people": people,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    return len(people)


async def import_school(path: Path, *, replace: bool = False) -> int:
    """Load a snapshot. Skipped when the school already has people, unless
    `replace`, which deletes the school first."""
    payload = json.loads(path.read_text())
    if payload.get("version") != SNAPSHOT_VERSION:
        raise SystemExit(f"{path} is not a version {SNAPSHOT_VERSION} snapshot")
    school = payload["school"]
    now = datetime.now(UTC)

    async with session_scope() as session:
        site = await session.scalar(
            select(Site).where(Site.root_domain == school["root_domain"])
        )
        if site is not None and replace:
            await session.delete(site)
            await session.flush()
            site = None
        if site is not None:
            has_people = await session.scalar(
                select(Record.id).where(Record.site_id == site.id).limit(1)
            )
            if has_people:
                return 0
        if site is None:
            site = Site(
                root_domain=school["root_domain"],
                canonical_url=school["canonical_url"],
                name=school.get("name"),
                hospital_name=school.get("hospital_name"),
                location=school.get("location"),
                institution_type=school.get("institution_type"),
                validation_status=ValidationStatus.POST_SECONDARY,
                validation_reason="Loaded from a crawl snapshot.",
            )
            session.add(site)
            await session.flush()

        captured = [datetime.fromisoformat(p["captured_at"]) for p in payload["people"]]
        run = Run(
            status=RunStatus.COMPLETED,
            label=f"snapshot of {school['root_domain']}",
            config={"source": "snapshot"},
            sites_total=1,
            sites_completed=1,
            started_at=min(captured, default=now),
            finished_at=max(captured, default=now),
            records_found=len(payload["people"]),
            records_new=len(payload["people"]),
        )
        session.add(run)
        await session.flush()
        site_run = SiteRun(
            run_id=run.id, site_id=site.id, status=SiteRunStatus.COMPLETED,
            step_budget=0, started_at=run.started_at, finished_at=run.finished_at,
            records_found=len(payload["people"]), records_new=len(payload["people"]),
        )
        session.add(site_run)
        site.last_scraped_at = run.finished_at
        await session.flush()

        by_page: dict[str, list[dict]] = defaultdict(list)
        for person in payload["people"]:
            by_page[person["source_url"]].append(person)

        await lock_site(session, site.id)
        total = 0
        for source_url, people in by_page.items():
            first = people[0]
            context = ExtractionContext(
                site_id=site.id,
                site_host=site.root_domain,
                source_url=source_url,
                page_title=first.get("page_title"),
                extraction_method=ExtractionMethod(first["extraction_method"]),
                fetch_mode=FetchMode(first["fetch_mode"]),
                run_id=run.id,
                site_run_id=site_run.id,
                captured_at=datetime.fromisoformat(first["captured_at"]),
            )
            extracted = [
                ExtractedPerson(
                    full_name=p.get("full_name"),
                    email=p.get("email"),
                    category=PersonCategory(p.get("category") or "unknown"),
                    position=p.get("position"),
                    pgy=p.get("pgy"),
                    class_of=p.get("class_of"),
                    specialty_raw=p.get("specialty"),
                    confidence=float(p.get("confidence") or 0.5),
                    source_note="snapshot",
                )
                for p in people
            ]
            outcome = await reconcile_people(session, extracted, context)
            total += outcome.total_seen
        await refresh_program_counts(session, site.id)
    return total
