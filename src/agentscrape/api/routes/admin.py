"""Admin: site table with success rates, platform aggregates, path management.

`/admin/stats` reports skip rate and known-path hit rate. Those two numbers say
whether the caching is earning its keep, and both should climb over time.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from ...db.enums import (
    RecordStatus,
    SiteRunStatus,
    ValidationStatus,
)
from ...db.models import (
    KnownPath,
    Record,
    RecordVersion,
    Run,
    Site,
    SiteRun,
)
from ...db.repositories.sites import delete_known_path
from ...domain.schemas import AdminSiteRow, AdminStats, Page
from ..deps import AuthedUser, DbSession
from ..errors import NotFoundError
from ..pagination import Cursor, clamp_limit

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/sites", response_model=Page[AdminSiteRow])
async def admin_sites(
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 100,
    q: Annotated[str | None, Query()] = None,
) -> Page[AdminSiteRow]:
    """Full site table with success rates and known-path counts."""
    run_stats = (
        select(
            SiteRun.site_id.label("site_id"),
            func.count(SiteRun.id).label("total"),
            func.sum(case((SiteRun.status == SiteRunStatus.COMPLETED, 1), else_=0)).label("ok"),
            func.sum(case((SiteRun.status == SiteRunStatus.SKIPPED, 1), else_=0)).label("skipped"),
            func.sum(case((SiteRun.status == SiteRunStatus.FAILED, 1), else_=0)).label("failed"),
        )
        .group_by(SiteRun.site_id)
        .subquery()
    )
    record_counts = (
        select(Record.site_id, func.count(Record.id).label("n"))
        .group_by(Record.site_id).subquery()
    )
    path_counts = (
        select(KnownPath.site_id, func.count(KnownPath.id).label("n"))
        .where(KnownPath.is_active.is_(True))
        .group_by(KnownPath.site_id).subquery()
    )

    statement = (
        select(
            Site,
            func.coalesce(record_counts.c.n, 0),
            func.coalesce(path_counts.c.n, 0),
            func.coalesce(run_stats.c.total, 0),
            func.coalesce(run_stats.c.ok, 0),
            func.coalesce(run_stats.c.skipped, 0),
            func.coalesce(run_stats.c.failed, 0),
        )
        .outerjoin(record_counts, record_counts.c.site_id == Site.id)
        .outerjoin(path_counts, path_counts.c.site_id == Site.id)
        .outerjoin(run_stats, run_stats.c.site_id == Site.id)
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

    items = []
    for site, records, paths, total, ok, skipped, failed in rows:
        items.append(
            AdminSiteRow(
                id=site.id,
                root_domain=site.root_domain,
                canonical_url=site.canonical_url,
                hospital=site.hospital_name,
                validation_status=site.validation_status,
                validation_reason=site.validation_reason,
                institution_type=site.institution_type,
                dominant_area=site.dominant_specialty,
                last_scraped_at=site.last_scraped_at,
                record_count=int(records),
                known_path_count=int(paths),
                total_site_runs=int(total),
                successful_site_runs=int(ok),
                skipped_site_runs=int(skipped),
                failed_site_runs=int(failed),
                success_rate=round(int(ok) / int(total), 3) if total else 0.0,
                skip_rate=round(int(skipped) / int(total), 3) if total else 0.0,
            )
        )

    next_cursor = (
        Cursor(sort_value=rows[-1][0].root_domain, id=rows[-1][0].id).encode()
        if has_more and rows
        else None
    )
    return Page[AdminSiteRow](items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/stats", response_model=AdminStats)
async def admin_stats(_: AuthedUser, session: DbSession) -> AdminStats:
    async def count(model, *where) -> int:
        statement = select(func.count()).select_from(model)
        for clause in where:
            statement = statement.where(clause)
        return int(await session.scalar(statement) or 0)

    site_runs_total = await count(SiteRun)
    skipped = await count(SiteRun, SiteRun.status == SiteRunStatus.SKIPPED)
    completed = await count(SiteRun, SiteRun.status == SiteRunStatus.COMPLETED)
    # Hit rate is measured over completed runs: a skipped site never reaches the
    # candidate list, so counting it would flatter the number.
    with_known_hit = await count(
        SiteRun, SiteRun.status == SiteRunStatus.COMPLETED, SiteRun.known_path_hits > 0
    )

    disk_bytes = 0
    from ...config import settings

    if settings.screenshot_dir.exists():
        disk_bytes = sum(
            f.stat().st_size for f in settings.screenshot_dir.rglob("*") if f.is_file()
        )

    return AdminStats(
        sites_total=await count(Site),
        sites_rejected=await count(
            Site, Site.validation_status == ValidationStatus.K12_REJECTED
        ),
        records_total=await count(Record),
        records_active=await count(
            Record, Record.status.in_([RecordStatus.ACTIVE, RecordStatus.NEW, RecordStatus.CHANGED])
        ),
        records_missing=await count(Record, Record.status == RecordStatus.MISSING),
        versions_total=await count(RecordVersion),
        runs_total=await count(Run),
        site_runs_total=site_runs_total,
        skip_rate=round(skipped / site_runs_total, 3) if site_runs_total else 0.0,
        known_path_hit_rate=round(with_known_hit / completed, 3) if completed else 0.0,
        known_paths_total=await count(KnownPath),
        known_paths_active=await count(KnownPath, KnownPath.is_active.is_(True)),
        total_spend_usd=float(await session.scalar(select(func.sum(Run.spend_usd))) or 0),
        total_tokens_in=int(await session.scalar(select(func.sum(Run.tokens_in))) or 0),
        total_tokens_out=int(await session.scalar(select(func.sum(Run.tokens_out))) or 0),
        screenshots_on_disk=await count(
            RecordVersion, RecordVersion.screenshot_available.is_(True)
        ),
        screenshots_expired=await count(
            RecordVersion,
            RecordVersion.screenshot_available.is_(False),
            RecordVersion.screenshot_path.is_(None),
        ),
        disk_bytes=disk_bytes,
    )


@router.delete("/sites/{site_id}/paths/{path_id}", status_code=204)
async def clear_known_path(
    site_id: str, path_id: str, _: AuthedUser, session: DbSession
) -> None:
    """Clear a stale known path manually."""
    if not await delete_known_path(session, site_id, path_id):
        raise NotFoundError(f"No known path {path_id!r} on site {site_id!r}.")
    await session.commit()


@router.post("/sweep")
async def sweep(_: AuthedUser) -> dict:
    """Run the retention sweep now (screenshots and exports past their window)."""
    from ...storage.artifacts import sweep_expired_exports, sweep_expired_screenshots

    return {
        "screenshots": await sweep_expired_screenshots(),
        "exports": await sweep_expired_exports(),
    }
