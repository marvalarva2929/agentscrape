"""Schools and programmes.

The frontend's primary navigation is School -> Person. Programmes remain as
internal grouping metadata, keyed on the normalized specialty.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from ...db.models import KnownPath, Program, Record, Site, SiteRun
from ...db.repositories import programs as programs_repo
from ...db.repositories.query import RecordFilters, query_records
from ...domain.schemas import (
    KnownPathOut,
    Page,
    ProgramOut,
    RecordOut,
    SchoolDetail,
    SchoolOut,
    SiteRunOut,
)
from ..deps import AuthedUser, DbSession
from ..errors import NotFoundError
from ..pagination import Cursor, clamp_limit

router = APIRouter(tags=["schools"])


def _school_out(site: Site, programs: int, people: int) -> SchoolOut:
    return SchoolOut(
        id=site.id,
        # Fall back to the domain so a school is never nameless in the UI.
        name=site.name or site.hospital_name or site.root_domain,
        location=site.location,
        root_domain=site.root_domain,
        canonical_url=site.canonical_url,
        program_count=programs,
        people_count=people,
        last_updated=site.last_scraped_at,
        validation_status=site.validation_status,
        validation_reason=site.validation_reason,
    )


def _program_out(program: Program) -> ProgramOut:
    return ProgramOut(
        id=program.id,
        school_id=program.site_id,
        name=program.name,
        specialty=program.specialty,
        type=program.program_type,
        resident_count=program.resident_count,
        fellow_count=program.fellow_count,
        people_count=program.people_count,
        last_updated=program.last_updated_at,
        start_url=program.start_url,
        directory_url=program.directory_url,
    )


@router.get("/schools", response_model=Page[SchoolOut])
async def list_schools(
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 100,
    q: Annotated[str | None, Query(description="Substring of name or domain")] = None,
) -> Page[SchoolOut]:
    program_counts = (
        select(Program.site_id, func.count(Program.id).label("n"))
        .group_by(Program.site_id).subquery()
    )
    people_counts = (
        select(Record.site_id, func.count(Record.id).label("n"))
        .group_by(Record.site_id).subquery()
    )
    statement = (
        select(
            Site,
            func.coalesce(program_counts.c.n, 0),
            func.coalesce(people_counts.c.n, 0),
        )
        .outerjoin(program_counts, program_counts.c.site_id == Site.id)
        .outerjoin(people_counts, people_counts.c.site_id == Site.id)
    )
    if q:
        statement = statement.where(
            Site.root_domain.ilike(f"%{q}%")
            | Site.name.ilike(f"%{q}%")
            | Site.hospital_name.ilike(f"%{q}%")
        )

    decoded = Cursor.decode(cursor)
    if decoded is not None:
        statement = statement.where(Site.root_domain > decoded.sort_value)

    limit = clamp_limit(limit)
    rows = (
        await session.execute(statement.order_by(Site.root_domain).limit(limit + 1))
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    return Page[SchoolOut](
        items=[_school_out(site, p, pe) for site, p, pe in rows],
        next_cursor=(
            Cursor(sort_value=rows[-1][0].root_domain, id=rows[-1][0].id).encode()
            if has_more and rows
            else None
        ),
        has_more=has_more,
    )


@router.get("/schools/{school_id}", response_model=SchoolDetail)
async def school_detail(
    school_id: str, _: AuthedUser, session: DbSession
) -> SchoolDetail:
    site = await session.get(Site, school_id)
    if site is None:
        raise NotFoundError(f"No school with id {school_id!r}.")

    people = int(
        await session.scalar(
            select(func.count(Record.id)).where(Record.site_id == school_id)
        ) or 0
    )
    program_rows = await programs_repo.list_programs(session, school_id)
    paths = (
        await session.execute(
            select(KnownPath)
            .where(KnownPath.site_id == school_id)
            .order_by(KnownPath.score.desc())
        )
    ).scalars().all()
    runs = (
        await session.execute(
            select(SiteRun)
            .where(SiteRun.site_id == school_id)
            .order_by(SiteRun.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    base = _school_out(site, len(program_rows), people)
    return SchoolDetail(
        **base.model_dump(),
        known_paths=[KnownPathOut.model_validate(p) for p in paths],
        recent_runs=[
            SiteRunOut.model_validate(r).model_copy(
                update={"domain": site.root_domain, "hospital": site.name}
            )
            for r in runs
        ],
    )


@router.get("/schools/{school_id}/programs", response_model=Page[ProgramOut])
async def list_school_programs(
    school_id: str, _: AuthedUser, session: DbSession
) -> Page[ProgramOut]:
    if await session.get(Site, school_id) is None:
        raise NotFoundError(f"No school with id {school_id!r}.")
    rows = await programs_repo.list_programs(session, school_id)
    return Page[ProgramOut](items=[_program_out(p) for p in rows], has_more=False)


@router.get("/schools/{school_id}/people", response_model=Page[RecordOut])
async def list_school_people(
    school_id: str,
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 100,
    category: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> Page[RecordOut]:
    if await session.get(Site, school_id) is None:
        raise NotFoundError(f"No school with id {school_id!r}.")

    filters = RecordFilters.from_query(site_id=[school_id], category=category, q=q)
    items, next_cursor, has_more = await query_records(
        session, filters, cursor=cursor, limit=limit
    )
    return Page[RecordOut](
        items=[RecordOut(**item) for item in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/programs/{program_id}", response_model=ProgramOut)
async def program_detail(
    program_id: str, _: AuthedUser, session: DbSession
) -> ProgramOut:
    program = await programs_repo.get_program(session, program_id)
    if program is None:
        raise NotFoundError(f"No programme with id {program_id!r}.")
    return _program_out(program)


@router.get("/programs/{program_id}/people", response_model=Page[RecordOut])
async def list_program_people(
    program_id: str,
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 100,
    category: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> Page[RecordOut]:
    if await programs_repo.get_program(session, program_id) is None:
        raise NotFoundError(f"No programme with id {program_id!r}.")

    filters = RecordFilters.from_query(program_id=program_id, category=category, q=q)
    items, next_cursor, has_more = await query_records(
        session, filters, cursor=cursor, limit=limit
    )
    return Page[RecordOut](
        items=[RecordOut(**item) for item in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )
