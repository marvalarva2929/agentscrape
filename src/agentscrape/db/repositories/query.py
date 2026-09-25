"""Record querying: filters, keyset pagination, aggregates.

`/records` and `/records/stats` take an identical filter set so the frontend
never aggregates client-side, and pagination is always keyset so pages stay
stable while a run is writing rows underneath them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.pagination import Cursor, clamp_limit
from ..enums import RecordStatus
from ..models import Record, RecordVersion, Site

SORTABLE = {
    "last_seen_at": Record.last_seen_at,
    "last_changed_at": Record.last_changed_at,
    "first_seen_at": Record.first_seen_at,
    "confidence": Record.confidence,
    "full_name": Record.full_name,
}
RECENTLY_CHANGED_DAYS = 30


@dataclass
class RecordFilters:
    """Filter set shared by /records, /records/stats and /records/export."""

    area: list[str] = field(default_factory=list)          # normalized specialty
    year: list[int] = field(default_factory=list)          # class-of year
    site_id: list[str] = field(default_factory=list)
    program_id: str | None = None
    status: list[str] = field(default_factory=list)
    category: list[str] = field(default_factory=list)      # resident/fellow/faculty/...
    pgy: list[int] = field(default_factory=list)           # additive: current PGY
    hospital: str | None = None                            # additive
    run_id: str | None = None
    changed_since: datetime | None = None
    q: str | None = None
    has_email: bool | None = None                          # additive
    include_role_accounts: bool = False

    @classmethod
    def from_query(cls, **kwargs: Any) -> RecordFilters:
        def as_list(value: Any) -> list:
            if value is None:
                return []
            return list(value) if isinstance(value, (list, tuple, set)) else [value]

        return cls(
            area=as_list(kwargs.get("area")),
            year=[int(y) for y in as_list(kwargs.get("year"))],
            site_id=as_list(kwargs.get("site_id")),
            program_id=kwargs.get("program_id"),
            status=as_list(kwargs.get("status")),
            category=as_list(kwargs.get("category")),
            pgy=[int(p) for p in as_list(kwargs.get("pgy"))],
            hospital=kwargs.get("hospital"),
            run_id=kwargs.get("run_id"),
            changed_since=kwargs.get("changed_since"),
            q=kwargs.get("q"),
            has_email=kwargs.get("has_email"),
            include_role_accounts=bool(kwargs.get("include_role_accounts")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "area": self.area, "year": self.year, "site_id": self.site_id,
            "program_id": self.program_id,
            "status": self.status, "category": self.category, "pgy": self.pgy,
            "hospital": self.hospital, "run_id": self.run_id,
            "changed_since": self.changed_since.isoformat() if self.changed_since else None,
            "q": self.q,
            "has_email": self.has_email,
            "include_role_accounts": self.include_role_accounts,
        }


def apply_filters(
    statement: Select, filters: RecordFilters, *, today: date | None = None
) -> Select:
    today = today or datetime.now(UTC).date()

    if filters.area:
        statement = statement.where(Record.specialty_normalized.in_(filters.area))
    if filters.year:
        statement = statement.where(Record.class_of.in_(filters.year))
    if filters.site_id:
        statement = statement.where(Record.site_id.in_(filters.site_id))
    if filters.program_id:
        statement = statement.where(Record.program_id == filters.program_id)
    if filters.status:
        statement = statement.where(Record.status.in_(filters.status))
    if filters.category:
        statement = statement.where(Record.category.in_(filters.category))
    if filters.run_id:
        statement = statement.where(Record.last_run_id == filters.run_id)
    if filters.changed_since:
        statement = statement.where(Record.last_changed_at >= filters.changed_since)
    if filters.has_email is True:
        statement = statement.where(Record.email.isnot(None))
    elif filters.has_email is False:
        statement = statement.where(Record.email.is_(None))
    if not filters.include_role_accounts:
        # Shared office inboxes are not people; hidden unless explicitly asked for.
        statement = statement.where(Record.role_account.is_(False))

    if filters.hospital:
        statement = statement.where(
            or_(
                Site.hospital_name.ilike(f"%{filters.hospital}%"),
                Site.root_domain.ilike(f"%{filters.hospital}%"),
            )
        )

    if filters.pgy:
        # Years are stored exactly as the page printed them and are never rolled
        # forward, so this is a straight match rather than a date translation.
        statement = statement.where(Record.pgy_at_capture.in_(filters.pgy))

    if filters.q:
        needle = filters.q.strip()
        if needle:
            statement = statement.where(
                or_(
                    func.to_tsvector(
                        "simple",
                        func.concat_ws(
                            " ",
                            func.coalesce(Record.full_name, ""),
                            func.coalesce(Record.email, ""),
                            func.coalesce(Record.specialty_normalized, ""),
                            func.coalesce(Record.specialty_raw, ""),
                        ),
                    ).op("@@")(func.plainto_tsquery("simple", needle)),
                    Record.full_name.ilike(f"%{needle}%"),
                    Record.email.ilike(f"%{needle}%"),
                )
            )

    return statement


DATETIME_SORTS = {"last_seen_at", "last_changed_at", "first_seen_at"}


def _coerce_sort_value(sort: str, value):
    """Cursors travel as JSON, so a timestamp comes back as an ISO string.

    Postgres will not compare `timestamptz < varchar`, so the value has to be
    parsed back into a datetime before it reaches the keyset predicate.
    """
    if sort in DATETIME_SORTS and isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


async def query_records(
    session: AsyncSession,
    filters: RecordFilters,
    *,
    cursor: str | None = None,
    limit: int | None = None,
    sort: str = "last_seen_at",
    descending: bool = True,
    record_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Return (rows, next_cursor, has_more). Keyset paginated; never OFFSET."""
    limit = clamp_limit(limit)
    column = SORTABLE.get(sort, Record.last_seen_at)

    statement = (
        select(Record, Site.hospital_name, Site.root_domain)
        .join(Site, Site.id == Record.site_id)
    )
    statement = apply_filters(statement, filters)
    if record_id:
        statement = statement.where(Record.id == record_id)

    decoded = Cursor.decode(cursor)
    if decoded is not None:
        sort_value = _coerce_sort_value(sort, decoded.sort_value)
        decoded = Cursor(sort_value=sort_value, id=decoded.id)
        # Tie-break on id so rows with an identical sort value cannot repeat or vanish.
        if descending:
            statement = statement.where(
                or_(
                    column < decoded.sort_value,
                    and_(column == decoded.sort_value, Record.id < decoded.id),
                )
            )
        else:
            statement = statement.where(
                or_(
                    column > decoded.sort_value,
                    and_(column == decoded.sort_value, Record.id > decoded.id),
                )
            )

    # Product decision: people who dropped off the site stay in the main table
    # but sort to the bottom, so the live roster reads first.
    missing_last = case((Record.status == RecordStatus.MISSING, 1), else_=0)
    order = (
        (missing_last, column.desc(), Record.id.desc())
        if descending
        else (missing_last, column.asc(), Record.id.asc())
    )
    statement = statement.order_by(*order).limit(limit + 1)

    result = (await session.execute(statement)).all()
    has_more = len(result) > limit
    rows = result[:limit]

    today = datetime.now(UTC).date()
    items = [_to_dict(record, hospital, domain, today) for record, hospital, domain in rows]

    next_cursor = None
    if has_more and rows:
        last_record = rows[-1][0]
        value = getattr(last_record, sort, None)
        next_cursor = Cursor(
            sort_value=value.isoformat() if isinstance(value, datetime) else value,
            id=last_record.id,
        ).encode()

    return items, next_cursor, has_more


def _to_dict(record: Record, hospital: str | None, domain: str, today: date) -> dict[str, Any]:
    return {
        "id": record.id,
        "site_id": record.site_id,
        "program_id": record.program_id,
        "hospital": hospital or domain,
        "full_name": record.full_name,
        "email": record.email,
        "category": record.category,
        "position": record.position,
        "role_account": record.role_account,
        "roles": record.roles,
        "roles_checked_at": record.roles_checked_at,
        "area": record.specialty_normalized,
        "area_raw": record.specialty_raw,
        "year": record.class_of,
        # Exactly as the page printed it, with the capture date beside it.
        "pgy": record.pgy_at_capture,
        "pgy_capture_date": record.pgy_capture_date,
        "status": record.status,
        "confidence": record.confidence,
        "verification_confidence": record.verification_confidence,
        "verification_risk": record.verification_risk,
        "verification_reason": record.verification_reason,
        "verification_evidence": record.verification_evidence,
        "verification_outcome": record.verification_outcome,
        "version_count": record.version_count,
        "first_seen_at": record.first_seen_at,
        "last_seen_at": record.last_seen_at,
        "last_changed_at": record.last_changed_at,
        "missing_since": record.missing_since,
    }


async def record_stats(session: AsyncSession, filters: RecordFilters) -> dict[str, Any]:
    """Aggregates over the same filter set the list endpoint uses."""
    base = select(Record.id).join(Site, Site.id == Record.site_id)
    ids = select(apply_filters(base, filters).subquery().c.id).subquery()

    async def scalar(statement) -> int:
        return int(await session.scalar(statement) or 0)

    total = await scalar(select(func.count()).select_from(ids))

    async def grouped(column) -> dict[str, int]:
        rows = await session.execute(
            select(column, func.count(Record.id))
            .where(Record.id.in_(select(ids.c.id)))
            .group_by(column)
        )
        return {str(key): int(count) for key, count in rows.all() if key is not None}

    cutoff = datetime.now(UTC) - timedelta(days=RECENTLY_CHANGED_DAYS)

    return {
        "total": total,
        "by_status": await grouped(Record.status),
        "by_category": await grouped(Record.category),
        "by_area": await grouped(Record.specialty_normalized),
        "sites_covered": await scalar(
            select(func.count(func.distinct(Record.site_id))).where(
                Record.id.in_(select(ids.c.id))
            )
        ),
        "recently_changed": await scalar(
            select(func.count(Record.id)).where(
                Record.id.in_(select(ids.c.id)), Record.last_changed_at >= cutoff
            )
        ),
        "with_email": await scalar(
            select(func.count(Record.id)).where(
                Record.id.in_(select(ids.c.id)), Record.email.isnot(None)
            )
        ),
        "average_confidence": round(
            float(
                await session.scalar(
                    select(func.avg(Record.confidence)).where(
                        Record.id.in_(select(ids.c.id))
                    )
                )
                or 0.0
            ),
            3,
        ),
        "average_versions": round(
            float(
                await session.scalar(
                    select(func.avg(Record.version_count)).where(
                        Record.id.in_(select(ids.c.id))
                    )
                )
                or 0.0
            ),
            2,
        ),
    }


async def distinct_areas(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        select(Record.specialty_normalized)
        .where(Record.specialty_normalized.isnot(None))
        .distinct()
        .order_by(Record.specialty_normalized)
    )
    return list(rows.scalars().all())


async def distinct_years(session: AsyncSession) -> list[int]:
    rows = await session.execute(
        select(Record.class_of)
        .where(Record.class_of.isnot(None))
        .distinct()
        .order_by(Record.class_of.desc())
    )
    return list(rows.scalars().all())


async def get_record(session: AsyncSession, record_id: str) -> Record | None:
    return await session.get(Record, record_id)


async def record_versions(
    session: AsyncSession, record_id: str
) -> list[RecordVersion]:
    rows = await session.execute(
        select(RecordVersion)
        .where(RecordVersion.record_id == record_id)
        .order_by(RecordVersion.version_no.desc())
    )
    return list(rows.scalars().all())


async def get_version(
    session: AsyncSession, record_id: str, version_id: str | None
) -> RecordVersion | None:
    if version_id:
        return await session.scalar(
            select(RecordVersion).where(
                RecordVersion.id == version_id, RecordVersion.record_id == record_id
            )
        )
    record = await session.get(Record, record_id)
    if record is None or not record.current_version_id:
        return None
    return await session.get(RecordVersion, record.current_version_id)


def status_counts_default() -> dict[str, int]:
    return {str(s): 0 for s in RecordStatus}
