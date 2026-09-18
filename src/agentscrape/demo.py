"""Demo data, so the app can be explored without crawling anything.

A first-time reviewer should be able to clone, run one script and see a
populated UI. Crawling a real institution takes minutes, hits live sites and
needs a model endpoint, none of which belong in a five-minute walkthrough.

The shapes here match what the crawler actually produces, including the awkward
parts: people with no email, people who have dropped off the site, derived-free
blank year fields, and a mix of trainees, faculty, staff and alumni.
"""

from __future__ import annotations

import logging
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db.enums import (
    ExtractionMethod,
    FetchMode,
    PersonCategory,
    RecordStatus,
    RunStatus,
    SiteRunStatus,
    ValidationStatus,
)
from .db.models import (
    KnownPath,
    Program,
    Record,
    RecordVersion,
    Run,
    Site,
    SiteRun,
)
from .db.repositories.programs import program_name_for
from .db.session import session_scope
from .storage.artifacts import relative_path
from .urls import url_hash

log = logging.getLogger("agentscrape.demo")

SCREENSHOT_WIDTH = 1200
SCREENSHOT_HEIGHT = 900

SCHOOLS: list[dict] = [
    {
        "domain": "med.example-university.edu",
        "name": "Example University School of Medicine",
        "location": "Chicago, IL",
        "programs": ["Internal Medicine", "Radiation Oncology", "General Surgery"],
    },
    {
        "domain": "health.northriver.edu",
        "name": "North River Health Sciences Center",
        "location": "Lubbock, TX",
        "programs": ["Pediatrics", "Psychiatry"],
    },
    {
        "domain": "medicine.baypoint.edu",
        "name": "Baypoint Medical College",
        "location": "Oakland, CA",
        "programs": ["Anesthesiology"],
    },
]

# (name, category, position, pgy, class_of, has_email, status)
PEOPLE_TEMPLATE: list[tuple] = [
    ("Jane A. Doe", PersonCategory.RESIDENT, "Resident", 2, None, True, RecordStatus.ACTIVE),
    ("Marcus Webb", PersonCategory.RESIDENT, "Chief Resident", 3, 2027, True, RecordStatus.ACTIVE),
    ("Sofia Almeida", PersonCategory.RESIDENT, "Resident", 1, None, False, RecordStatus.NEW),
    ("Tom O'Brien", PersonCategory.FELLOW, "Clinical Fellow", None, 2028, True, RecordStatus.ACTIVE),
    ("Priya Raman", PersonCategory.FELLOW, "Fellow", None, None, True, RecordStatus.NEW),
    ("Alan Grant", PersonCategory.FACULTY, "Program Director", None, None, True, RecordStatus.ACTIVE),
    ("Ellie Sattler", PersonCategory.FACULTY, "Associate Professor", None, None, True, RecordStatus.ACTIVE),
    ("Ray Arnold", PersonCategory.STAFF, "Program Coordinator", None, None, True, RecordStatus.ACTIVE),
    ("Tim Murphy", PersonCategory.STUDENT, "Medical Student", None, None, False, RecordStatus.ACTIVE),
    ("Dana Fields", PersonCategory.ALUMNI, None, None, 2024, False, RecordStatus.ACTIVE),
    # Someone who has dropped off the site: kept, never deleted.
    ("Owen Cole", PersonCategory.RESIDENT, "Resident", 3, None, True, RecordStatus.MISSING),
]


def _placeholder_png(width: int, height: int) -> bytes:
    """A small valid PNG, so the provenance panel has something to render.

    Hand-rolled rather than pulling in an image library for one fixture.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # One light-grey row per scanline, each prefixed with a filter byte.
    row = b"\x00" + b"\xf2\xf4\xf7" * width
    pixels = zlib.compress(row * height, 6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


def _write_screenshot(site_run_id: str, key: str) -> str | None:
    settings.ensure_dirs()
    directory = settings.screenshot_dir / site_run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{url_hash(key)[:32]}.png"
    path.write_bytes(_placeholder_png(SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT))
    return relative_path(path)


def snapshot_dir() -> Path:
    """The configured snapshot folder, else demo/ in the repository or the
    working directory (an installed package has no repository beside it)."""
    if settings.demo_snapshot_dir:
        return Path(settings.demo_snapshot_dir)
    for candidate in (Path(__file__).resolve().parents[2] / "demo", Path.cwd() / "demo"):
        if candidate.is_dir():
            return candidate
    return Path.cwd() / "demo"


async def seed_if_empty() -> int:
    """Seed the demo snapshots when no school exists yet. Returns people loaded."""
    from sqlalchemy import func

    async with session_scope() as session:
        schools = await session.scalar(select(func.count(Site.id)))
    if schools:
        return 0
    folder = snapshot_dir()
    snapshots = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not snapshots:
        log.warning("database is empty and no demo snapshots were found in %s", folder)
        return 0
    counts = await _seed_snapshots(snapshots, reset=False)
    log.info("seeded an empty database: %d schools, %d people", counts["schools"], counts["people"])
    return counts["people"]


async def seed_demo(*, reset: bool = False) -> dict[str, int]:
    """Populate the app with something to show. Idempotent.

    Real crawl snapshots in `demo/*.json` (see `agentscrape export-school`) are
    preferred: a demo should open on an actual institution's results. The
    invented schools below are the fallback when no snapshot is present.
    """
    folder = snapshot_dir()
    snapshots = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if snapshots:
        return await _seed_snapshots(snapshots, reset=reset)
    return await _seed_invented(reset=reset)


async def _seed_snapshots(paths: list[Path], *, reset: bool) -> dict[str, int]:
    from .snapshot import import_school

    if reset:
        await _truncate()
    counts = {"schools": 0, "programs": 0, "people": 0}
    for path in paths:
        people = await import_school(path)
        if people:
            counts["schools"] += 1
            counts["people"] += people
    async with session_scope() as session:
        from sqlalchemy import func

        counts["programs"] = int(await session.scalar(select(func.count(Program.id))) or 0)
    return counts


async def _truncate() -> None:
    from sqlalchemy import text

    async with session_scope() as session:
        await session.execute(
            text(
                "TRUNCATE sites, runs, site_runs, site_run_visits, records, "
                "record_versions, known_paths, exports, programs, "
                "csv_submissions RESTART IDENTITY CASCADE"
            )
        )


async def _seed_invented(*, reset: bool = False) -> dict[str, int]:
    """Populate invented schools, programmes, people and provenance."""
    now = datetime.now(UTC)
    counts = {"schools": 0, "programs": 0, "people": 0}

    if reset:
        await _truncate()

    async with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETED,
            label="demo data",
            config={"source": "seed", "concurrency": 1},
            sites_total=len(SCHOOLS),
            sites_completed=len(SCHOOLS),
            started_at=now - timedelta(minutes=12),
            finished_at=now - timedelta(minutes=4),
            spend_usd=1.84,
            tokens_in=412_000,
            tokens_out=38_500,
        )
        session.add(run)
        await session.flush()

        for school_index, spec in enumerate(SCHOOLS):
            existing = await session.scalar(
                select(Site).where(Site.root_domain == spec["domain"])
            )
            if existing is not None:
                continue

            site = Site(
                root_domain=spec["domain"],
                canonical_url=f"https://{spec['domain']}/",
                name=spec["name"],
                location=spec["location"],
                hospital_name=spec["name"],
                validation_status=ValidationStatus.POST_SECONDARY,
                validation_reason="Seeded demo data.",
                institution_type="academic_medical_center",
                last_scraped_at=now - timedelta(days=school_index),
            )
            session.add(site)
            await session.flush()
            counts["schools"] += 1

            site_run = SiteRun(
                run_id=run.id,
                site_id=site.id,
                status=SiteRunStatus.COMPLETED,
                agent_id="agent-0",
                steps_taken=18,
                records_found=len(PEOPLE_TEMPLATE) * len(spec["programs"]),
                started_at=now - timedelta(minutes=12),
                finished_at=now - timedelta(minutes=4),
            )
            session.add(site_run)
            await session.flush()

            session.add(
                KnownPath(
                    site_id=site.id,
                    url=f"https://{spec['domain']}/residents",
                    url_hash=url_hash(f"https://{spec['domain']}/residents"),
                    success_count=3,
                    avg_records=len(PEOPLE_TEMPLATE),
                    score=21.0,
                    last_success_at=now - timedelta(days=school_index),
                    is_active=True,
                )
            )

            for specialty in spec["programs"]:
                program = Program(
                    site_id=site.id,
                    specialty=specialty,
                    name=program_name_for(specialty),
                    start_url=f"https://{spec['domain']}/{specialty.lower().replace(' ', '-')}",
                    directory_url=f"https://{spec['domain']}/{specialty.lower().replace(' ', '-')}/people",
                    last_updated_at=now - timedelta(days=school_index),
                )
                session.add(program)
                await session.flush()
                counts["programs"] += 1

                source_url = f"{program.directory_url}"
                screenshot = _write_screenshot(site_run.id, f"{site.id}:{specialty}")

                people_total = residents = fellows = 0
                for person_index, (
                    name, category, position, pgy, class_of, has_email, status,
                ) in enumerate(PEOPLE_TEMPLATE):
                    local = ".".join(
                        part.strip(".").lower().replace("'", "")
                        for part in name.split()
                        if part.strip(".")
                    )
                    email = f"{local}@{spec['domain']}" if has_email else None
                    identity = f"email:{email}" if email else f"name:{name.lower()}|{category}"

                    record = Record(
                        site_id=site.id,
                        program_id=program.id,
                        identity_key=f"{identity}:{specialty}",
                        identity_kind="email" if email else "name",
                        full_name=name,
                        email=email,
                        category=category,
                        position=position,
                        specialty_normalized=specialty,
                        specialty_raw=specialty,
                        pgy_at_capture=pgy,
                        pgy_capture_date=(now - timedelta(days=school_index)).date(),
                        class_of=class_of,
                        status=status,
                        confidence=0.95 if email else 0.6,
                        version_count=1,
                        first_seen_at=now - timedelta(days=45),
                        last_seen_at=now - timedelta(days=school_index),
                        last_changed_at=now - timedelta(days=school_index),
                        missing_since=(
                            now - timedelta(days=2)
                            if status == RecordStatus.MISSING
                            else None
                        ),
                        last_run_id=run.id,
                    )
                    session.add(record)
                    await session.flush()

                    version = RecordVersion(
                        record_id=record.id,
                        version_no=1,
                        fields={
                            "full_name": name,
                            "email": email,
                            "category": str(category),
                            "position": position,
                        },
                        changed_fields={},
                        source_url=source_url,
                        page_title=f"{program.name} | {spec['name']}",
                        screenshot_path=screenshot,
                        screenshot_available=screenshot is not None,
                        screenshot_width=SCREENSHOT_WIDTH,
                        screenshot_height=SCREENSHOT_HEIGHT,
                        captured_at=now - timedelta(days=school_index),
                        extraction_method=ExtractionMethod.DISCOVERY,
                        fetch_mode=FetchMode.BOTH,
                        confidence=record.confidence,
                        # Boxes in screenshot pixel space, as the crawler stores them.
                        field_locations={
                            "full_name": {
                                "x": 120,
                                "y": 150 + person_index * 60,
                                "width": 240,
                                "height": 24,
                            },
                            **(
                                {
                                    "email": {
                                        "x": 420,
                                        "y": 152 + person_index * 60,
                                        "width": 320,
                                        "height": 20,
                                    }
                                }
                                if email
                                else {}
                            ),
                        },
                        run_id=run.id,
                        site_run_id=site_run.id,
                    )
                    session.add(version)
                    await session.flush()
                    record.current_version_id = version.id
                    counts["people"] += 1

                    if status != RecordStatus.MISSING:
                        people_total += 1
                        if category == PersonCategory.RESIDENT:
                            residents += 1
                        elif category == PersonCategory.FELLOW:
                            fellows += 1

                    # One person has a change history, so the timeline is not empty.
                    if person_index == 0:
                        session.add(
                            RecordVersion(
                                record_id=record.id,
                                version_no=2,
                                fields={"full_name": name, "email": email},
                                changed_fields={
                                    "email": {
                                        "from": f"j.doe@{spec['domain']}",
                                        "to": email,
                                    }
                                },
                                source_url=source_url,
                                page_title=f"{program.name} | {spec['name']}",
                                screenshot_path=screenshot,
                                screenshot_available=screenshot is not None,
                                screenshot_width=SCREENSHOT_WIDTH,
                                screenshot_height=SCREENSHOT_HEIGHT,
                                captured_at=now - timedelta(days=school_index),
                                extraction_method=ExtractionMethod.KNOWN_PATH,
                                fetch_mode=FetchMode.HTML,
                                confidence=0.95,
                                run_id=run.id,
                                site_run_id=site_run.id,
                            )
                        )
                        record.version_count = 2

                program.people_count = people_total
                program.resident_count = residents
                program.fellow_count = fellows

            run.records_found += counts["people"]

        await session.commit()

    log.info(
        "seeded %d schools, %d programmes, %d people",
        counts["schools"], counts["programs"], counts["people"],
    )
    return counts
