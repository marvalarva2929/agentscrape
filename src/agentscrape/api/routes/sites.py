"""Site listing and detail, plus the meta endpoints that back frontend filters."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from ...db.models import KnownPath, Record, Site, SiteRun
from ...db.repositories.query import distinct_areas, distinct_years
from ...domain.schemas import (
    KnownPathOut,
    MetaValues,
    Page,
    SiteDetail,
    SiteOut,
    SiteRunOut,
)
from ..deps import AuthedUser, DbSession
from ..errors import NotFoundError
from ..pagination import Cursor, clamp_limit

router = APIRouter(tags=["sites"])


def _counts_subqueries():
    record_counts = (
        select(Record.site_id, func.count(Record.id).label("n"))
        .group_by(Record.site_id)
        .subquery()
    )
    path_counts = (
        select(KnownPath.site_id, func.count(KnownPath.id).label("n"))
        .where(KnownPath.is_active.is_(True))
        .group_by(KnownPath.site_id)
        .subquery()
    )
    return record_counts, path_counts


@router.get("/sites", response_model=Page[SiteOut])
async def list_sites(
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 50,
    q: Annotated[str | None, Query(description="Substring of domain or hospital")] = None,
) -> Page[SiteOut]:
    """Site list for typeahead and filters."""
    record_counts, path_counts = _counts_subqueries()
    statement = (
        select(
            Site,
            func.coalesce(record_counts.c.n, 0),
            func.coalesce(path_counts.c.n, 0),
        )
        .outerjoin(record_counts, record_counts.c.site_id == Site.id)
        .outerjoin(path_counts, path_counts.c.site_id == Site.id)
    )
    if q:
        statement = statement.where(
            Site.root_domain.ilike(f"%{q}%") | Site.hospital_name.ilike(f"%{q}%")
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

    items = [_site_out(site, records, paths) for site, records, paths in rows]
    next_cursor = (
        Cursor(sort_value=rows[-1][0].root_domain, id=rows[-1][0].id).encode()
        if has_more and rows
        else None
    )
    return Page[SiteOut](items=items, next_cursor=next_cursor, has_more=has_more)


def _site_out(site: Site, record_count: int, path_count: int) -> SiteOut:
    return SiteOut(
        id=site.id,
        root_domain=site.root_domain,
        canonical_url=site.canonical_url,
        hospital=site.hospital_name,
        validation_status=site.validation_status,
        validation_reason=site.validation_reason,
        institution_type=site.institution_type,
        dominant_area=site.dominant_specialty,
        last_scraped_at=site.last_scraped_at,
        record_count=record_count,
        known_path_count=path_count,
    )


@router.get("/sites/{site_id}", response_model=SiteDetail)
async def site_detail(site_id: str, _: AuthedUser, session: DbSession) -> SiteDetail:
    site = await session.get(Site, site_id)
    if site is None:
        raise NotFoundError(f"No site with id {site_id!r}.")

    record_count = int(
        await session.scalar(
            select(func.count(Record.id)).where(Record.site_id == site_id)
        ) or 0
    )
    paths = (
        await session.execute(
            select(KnownPath)
            .where(KnownPath.site_id == site_id)
            .order_by(KnownPath.score.desc())
        )
    ).scalars().all()
    runs = (
        await session.execute(
            select(SiteRun)
            .where(SiteRun.site_id == site_id)
            .order_by(SiteRun.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    base = _site_out(site, record_count, sum(1 for p in paths if p.is_active))
    return SiteDetail(
        **base.model_dump(),
        known_paths=[KnownPathOut.model_validate(p) for p in paths],
        recent_runs=[
            SiteRunOut.model_validate(r).model_copy(
                update={"domain": site.root_domain, "hospital": site.hospital_name}
            )
            for r in runs
        ],
    )


meta_router = APIRouter(prefix="/meta", tags=["meta"])


@meta_router.get("/areas", response_model=MetaValues)
async def areas(_: AuthedUser, session: DbSession) -> MetaValues:
    """Distinct specialties present in the data (`area` in the contract)."""
    return MetaValues(values=list(await distinct_areas(session)))


@meta_router.get("/years", response_model=MetaValues)
async def years(_: AuthedUser, session: DbSession) -> MetaValues:
    """Distinct class-of years present in the data (`year` in the contract)."""
    return MetaValues(values=list(await distinct_years(session)))


@meta_router.get("/roles", response_model=MetaValues)
async def roles(_: AuthedUser, session: DbSession) -> MetaValues:
    """Distinct roles. Additive to the contract; backs the R/F filter."""
    rows = await session.execute(select(Record.role).distinct().order_by(Record.role))
    return MetaValues(values=list(rows.scalars().all()))
