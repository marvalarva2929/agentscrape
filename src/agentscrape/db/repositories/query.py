"""Record querying: filters, keyset pagination, aggregates.

`/records` and `/records/stats` take an identical filter set so the frontend
never aggregates client-side, and pagination is always keyset so pages stay
stable while a run is writing rows underneath them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.pagination import Cursor, clamp_limit
from ...domain.pgy import current_pgy
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
    status: list[str] = field(default_factory=list)
    role: list[str] = field(default_factory=list)          # additive: R/F/unknown
    pgy: list[int] = field(default_factory=list)           # additive: current PGY
    hospital: str | None = None                            # additive
    run_id: str | None = None
    changed_since: datetime | None = None
    q: str | None = None
    has_screenshot: bool | None = None
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
            status=as_list(kwargs.get("status")),
            role=as_list(kwargs.get("role")),
            pgy=[int(p) for p in as_list(kwargs.get("pgy"))],
            hospital=kwargs.get("hospital"),
            run_id=kwargs.get("run_id"),
            changed_since=kwargs.get("changed_since"),
            q=kwargs.get("q"),
            has_screenshot=kwargs.get("has_screenshot"),
            has_email=kwargs.get("has_email"),
            include_role_accounts=bool(kwargs.get("include_role_accounts")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "area": self.area, "year": self.year, "site_id": self.site_id,
            "status": self.status, "role": self.role, "pgy": self.pgy,
            "hospital": self.hospital, "run_id": self.run_id,
            "changed_since": self.changed_since.isoformat() if self.changed_since else None,
            "q": self.q, "has_screenshot": self.has_screenshot,
            "has_email": self.has_email,
            "include_role_accounts": self.include_role_accounts,
        }


def _pgy_capture_bounds(target_pgy: int, today: date) -> list[tuple[int, int, int]]:
    """PGY is derived, so filtering on it means translating back to storage.

    A record matches `pgy = n` when pgy_at_capture + (AY(today) - AY(captured))
    equals n. Expanded into concrete (stored_pgy, academic_year) pairs so the
    filter runs as an indexed SQL predicate rather than in Python.
    """
    from ...domain.pgy import ACADEMIC_YEAR_START_MONTH, academic_year

    current_ay = academic_year(today)
    pairs: list[tuple[int, int, int]] = []
    # Captures older than nine academic years can never still be in-programme.
    for offset in range(0, 10):
        stored = target_pgy - offset
        if stored < 1:
            break
        capture_ay = current_ay - offset
        pairs.append((stored, capture_ay, ACADEMIC_YEAR_START_MONTH))
    return pairs


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
    if filters.status:
        statement = statement.where(Record.status.in_(filters.status))
    if filters.role:
        statement = statement.where(Record.role.in_(filters.role))
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
        clauses = []
        for target in filters.pgy:
            for stored, capture_ay, start_month in _pgy_capture_bounds(target, today):
                clauses.append(
                    and_(
                        Record.pgy_at_capture == stored,
                        Record.pgy_capture_date >= date(capture_ay, start_month, 1),
                        Record.pgy_capture_date < date(capture_ay + 1, start_month, 1),
                    )
                )
        statement = statement.where(or_(*clauses) if clauses else text("false"))

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

    if filters.has_screenshot is not None:
        # correlate(Record) is required: without it SQLAlchemy correlates the
        # version table too and the subquery is left with no FROM clause.
        subquery = (
            select(RecordVersion.id)
            .where(
                RecordVersion.id == Record.current_version_id,
                RecordVersion.screenshot_available.is_(True),
            )
            .correlate(Record)
            .exists()
        )
        statement = statement.where(subquery if filters.has_screenshot else ~subquery)

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
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Return (rows, next_cursor, has_more). Keyset paginated; never OFFSET."""
    limit = clamp_limit(limit)
    column = SORTABLE.get(sort, Record.last_seen_at)

    statement = (
        select(
            Record,
            Site.hospital_name,
            Site.root_domain,
            RecordVersion.screenshot_available,
        )
        .join(Site, Site.id == Record.site_id)
        .outerjoin(RecordVersion, RecordVersion.id == Record.current_version_id)
    )
    statement = apply_filters(statement, filters)

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

    order = (column.desc(), Record.id.desc()) if descending else (column.asc(), Record.id.asc())
    statement = statement.order_by(*order).limit(limit + 1)

    result = (await session.execute(statement)).all()
    has_more = len(result) > limit
    rows = result[:limit]

    today = datetime.now(UTC).date()
    items = [
        _to_dict(record, hospital, domain, bool(has_shot), today)
        for record, hospital, domain, has_shot in rows
    ]

    next_cursor = None
    if has_more and rows:
        last_record = rows[-1][0]
        value = getattr(last_record, sort, None)
        next_cursor = Cursor(
            sort_value=value.isoformat() if isinstance(value, datetime) else value,
            id=last_record.id,
        ).encode()

    return items, next_cursor, has_more


def _to_dict(
    record: Record, hospital: str | None, domain: str, has_screenshot: bool, today: date
) -> dict[str, Any]:
    return {
        "id": record.id,
        "site_id": record.site_id,
        "hospital": hospital or domain,
        "full_name": record.full_name,
        "email": record.email,
        "role": record.role,
        "role_account": record.role_account,
        "area": record.specialty_normalized,
        "area_raw": record.specialty_raw,
        "year": record.class_of,
        "year_source": record.class_of_source,
        # Derived on read so a saved PGY filter keeps meaning "PGY-n today".
        "pgy": current_pgy(record.pgy_at_capture, record.pgy_capture_date, today=today),
        "pgy_at_capture": record.pgy_at_capture,
        "pgy_capture_date": record.pgy_capture_date,
        "pgy_source": record.pgy_source,
        "status": record.status,
        "confidence": record.confidence,
        "version_count": record.version_count,
        "first_seen_at": record.first_seen_at,
        "last_seen_at": record.last_seen_at,
        "last_changed_at": record.last_changed_at,
        "missing_since": record.missing_since,
        "screenshot_available": has_screenshot,
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
        "by_role": await grouped(Record.role),
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
        "with_screenshot": await scalar(
            select(func.count(Record.id))
            .join(RecordVersion, RecordVersion.id == Record.current_version_id)
            .where(
                Record.id.in_(select(ids.c.id)),
                RecordVersion.screenshot_available.is_(True),
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
