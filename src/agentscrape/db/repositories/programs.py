"""Programme persistence.

A programme is one training programme at a school — "Internal Medicine
Residency" at a given institution — keyed on (site, normalized specialty).

Rows appear automatically as people are extracted, so browsing works without
anyone curating them, and they can also be created directly so a newly added
school can be given start/directory URLs before it has been crawled.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..enums import PersonCategory
from ..ids import program_id as new_program_id
from ..models import Program, Record

log = logging.getLogger("agentscrape.programs")

# How a specialty is turned into a programme name for display.
RESIDENCY_SUFFIX = "Residency"


def program_name_for(specialty: str) -> str:
    """"Internal Medicine" -> "Internal Medicine Residency"."""
    if specialty.lower().endswith(("residency", "fellowship", "program")):
        return specialty
    return f"{specialty} {RESIDENCY_SUFFIX}"


async def ensure_program(
    session: AsyncSession, *, site_id: str, specialty: str
) -> str:
    """Get or create the programme for a (site, specialty) pair; return its id.

    Uses an upsert because several agents can reconcile people from the same
    programme concurrently.
    """
    statement = (
        pg_insert(Program)
        .values(
            id=new_program_id(),
            site_id=site_id,
            specialty=specialty,
            name=program_name_for(specialty),
        )
        .on_conflict_do_nothing(index_elements=["site_id", "specialty"])
        .returning(Program.id)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    if created is not None:
        return created

    existing = await session.scalar(
        select(Program.id).where(
            Program.site_id == site_id, Program.specialty == specialty
        )
    )
    return existing  # type: ignore[return-value]


async def refresh_program_counts(session: AsyncSession, site_id: str) -> int:
    """Recompute people/resident/fellow counts for a site's programmes.

    Run once at the end of a site's crawl rather than per record, so counts are
    consistent with what was actually stored.
    """
    from ..enums import RecordStatus

    rows = (
        await session.execute(
            select(
                Record.program_id,
                func.count(Record.id),
                func.count(Record.id).filter(
                    Record.category == PersonCategory.RESIDENT
                ),
                func.count(Record.id).filter(Record.category == PersonCategory.FELLOW),
            )
            .where(
                Record.site_id == site_id,
                Record.program_id.isnot(None),
                Record.status != RecordStatus.MISSING,
            )
            .group_by(Record.program_id)
        )
    ).all()

    now = datetime.now(UTC)
    for program_id, people, residents, fellows in rows:
        program = await session.get(Program, program_id)
        if program is None:
            continue
        program.people_count = int(people)
        program.resident_count = int(residents)
        program.fellow_count = int(fellows)
        program.last_updated_at = now

    log.info("refreshed counts for %d programmes on site %s", len(rows), site_id)
    return len(rows)


async def list_programs(session: AsyncSession, site_id: str) -> list[Program]:
    rows = await session.execute(
        select(Program).where(Program.site_id == site_id).order_by(Program.name)
    )
    return list(rows.scalars().all())


async def get_program(session: AsyncSession, program_id: str) -> Program | None:
    return await session.get(Program, program_id)


async def set_program_urls(
    session: AsyncSession,
    program_id: str,
    *,
    start_url: str | None = None,
    directory_url: str | None = None,
) -> Program | None:
    """Record the entry points used to re-crawl just this programme."""
    program = await session.get(Program, program_id)
    if program is None:
        return None
    if start_url is not None:
        program.start_url = start_url
    if directory_url is not None:
        program.directory_url = directory_url
    return program
