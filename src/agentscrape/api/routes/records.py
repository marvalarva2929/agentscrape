"""People: query, stats, single person, versions, provenance and export.

Named "people" to match the frontend's domain language. Internally a person is a
`Record` with a `RecordVersion` history.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from ...db.repositories import query as q
from ...db.repositories.query import RecordFilters, query_records
from ...domain.schemas import (
    ExportCreate,
    ExportOut,
    FieldChange,
    Page,
    RecordOut,
    RecordStats,
    RecordVersionOut,
    SourceOut,
)
from ...storage.artifacts import absolute_path
from ..deps import AuthedUser, DbSession
from ..errors import AppError, ErrorCode, NotFoundError
from ..security import sign_artifact_path

router = APIRouter(tags=["people"])


def _filters_from_query(
    area: Annotated[list[str] | None, Query(description="Normalized specialty")] = None,
    year: Annotated[list[int] | None, Query(description="Class-of year")] = None,
    site_id: Annotated[list[str] | None, Query()] = None,
    status: Annotated[list[str] | None, Query()] = None,
    category: Annotated[list[str] | None, Query(
        description="resident|fellow|faculty|staff|student|alumni|unknown"
    )] = None,
    pgy: Annotated[list[int] | None, Query(description="Current PGY level")] = None,
    hospital: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    changed_since: Annotated[datetime | None, Query()] = None,
    q_: Annotated[str | None, Query(alias="q", description="Free text")] = None,
    has_screenshot: Annotated[bool | None, Query()] = None,
    has_email: Annotated[bool | None, Query()] = None,
    include_role_accounts: Annotated[bool, Query()] = False,
) -> q.RecordFilters:
    return q.RecordFilters.from_query(
        area=area, year=year, site_id=site_id, status=status, category=category, pgy=pgy,
        hospital=hospital, run_id=run_id, changed_since=changed_since, q=q_,
        has_screenshot=has_screenshot, has_email=has_email,
        include_role_accounts=include_role_accounts,
    )


Filters = Annotated[q.RecordFilters, Query()]


@router.get("/people", response_model=Page[RecordOut])
async def list_records(
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 50,
    sort: str = "last_seen_at",
    order: str = "desc",
    area: Annotated[list[str] | None, Query()] = None,
    year: Annotated[list[int] | None, Query()] = None,
    site_id: Annotated[list[str] | None, Query()] = None,
    status: Annotated[list[str] | None, Query()] = None,
    category: Annotated[list[str] | None, Query()] = None,
    pgy: Annotated[list[int] | None, Query()] = None,
    hospital: str | None = None,
    run_id: str | None = None,
    changed_since: datetime | None = None,
    q_: Annotated[str | None, Query(alias="q")] = None,
    has_screenshot: bool | None = None,
    has_email: bool | None = None,
    include_role_accounts: bool = False,
) -> Page[RecordOut]:
    filters = _filters_from_query(
        area, year, site_id, status, category, pgy, hospital, run_id, changed_since,
        q_, has_screenshot, has_email, include_role_accounts,
    )
    items, next_cursor, has_more = await q.query_records(
        session, filters, cursor=cursor, limit=limit, sort=sort,
        descending=order.lower() != "asc",
    )
    return Page[RecordOut](
        items=[RecordOut(**item) for item in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/people/stats", response_model=RecordStats)
async def records_stats(
    _: AuthedUser,
    session: DbSession,
    area: Annotated[list[str] | None, Query()] = None,
    year: Annotated[list[int] | None, Query()] = None,
    site_id: Annotated[list[str] | None, Query()] = None,
    status: Annotated[list[str] | None, Query()] = None,
    category: Annotated[list[str] | None, Query()] = None,
    pgy: Annotated[list[int] | None, Query()] = None,
    hospital: str | None = None,
    run_id: str | None = None,
    changed_since: datetime | None = None,
    q_: Annotated[str | None, Query(alias="q")] = None,
    has_screenshot: bool | None = None,
    has_email: bool | None = None,
    include_role_accounts: bool = False,
) -> RecordStats:
    filters = _filters_from_query(
        area, year, site_id, status, category, pgy, hospital, run_id, changed_since,
        q_, has_screenshot, has_email, include_role_accounts,
    )
    return RecordStats(**await q.record_stats(session, filters))


def _changes(raw: dict[str, Any] | None) -> list[FieldChange]:
    return [
        FieldChange(field=name, previous=delta.get("from"), current=delta.get("to"))
        for name, delta in (raw or {}).items()
    ]


def _screenshot_url(version) -> str | None:
    """A signed, expiring link the browser can put straight in an <img> tag."""
    if not version.screenshot_available or not version.screenshot_path:
        return None
    query = sign_artifact_path(version.screenshot_path)
    return f"/api/v1/artifacts/{version.screenshot_path}?{query}"


@router.get("/people/{record_id}", response_model=RecordOut)
async def get_person(record_id: str, _: AuthedUser, session: DbSession) -> RecordOut:
    """One person. The frontend opens this from a table row."""
    filters = RecordFilters.from_query(include_role_accounts=True)
    items, _cursor, _more = await query_records(
        session, filters, record_id=record_id, limit=1
    )
    if not items:
        raise NotFoundError(f"No person with id {record_id!r}.")
    return RecordOut(**items[0])


@router.get("/people/{record_id}/versions", response_model=list[RecordVersionOut])
async def record_versions(
    record_id: str, _: AuthedUser, session: DbSession
) -> list[RecordVersionOut]:
    record = await q.get_record(session, record_id)
    if record is None:
        raise NotFoundError(f"No person with id {record_id!r}.")
    return [
        RecordVersionOut(
            id=v.id, record_id=v.record_id, version_no=v.version_no, fields=v.fields,
            changed_fields=_changes(v.changed_fields), captured_at=v.captured_at,
            confidence=v.confidence, source_url=v.source_url, page_title=v.page_title,
            extraction_method=v.extraction_method, fetch_mode=v.fetch_mode,
            screenshot_available=v.screenshot_available,
            screenshot_url=_screenshot_url(v),
            screenshot_expires_at=v.screenshot_expires_at,
            field_locations=v.field_locations, run_id=v.run_id,
        )
        for v in await q.record_versions(session, record_id)
    ]


@router.get("/people/{record_id}/source", response_model=SourceOut)
async def record_source(
    record_id: str,
    _: AuthedUser,
    session: DbSession,
    version_id: str | None = None,
) -> SourceOut:
    """Provenance for one record version; defaults to the current version.

    URL, title, timestamp, method and field locations survive screenshot expiry;
    `screenshot_available` tells the frontend which state it is rendering.
    """
    record = await q.get_record(session, record_id)
    if record is None:
        raise NotFoundError(f"No person with id {record_id!r}.")
    version = await q.get_version(session, record_id, version_id)
    if version is None:
        raise NotFoundError(
            f"No version found for record {record_id!r}.",
            details={"version_id": version_id},
        )
    return SourceOut(
        record_id=record_id,
        version_id=version.id,
        version_no=version.version_no,
        source_url=version.source_url,
        page_title=version.page_title,
        captured_at=version.captured_at,
        extraction_method=version.extraction_method,
        fetch_mode=version.fetch_mode,
        confidence=version.confidence,
        screenshot_available=version.screenshot_available,
        screenshot_url=_screenshot_url(version),
        screenshot_expires_at=version.screenshot_expires_at,
        screenshot_width=version.screenshot_width,
        screenshot_height=version.screenshot_height,
        field_locations=version.field_locations,
        run_id=version.run_id,
    )


@router.post("/people/export", response_model=ExportOut, status_code=202)
async def start_export(
    body: ExportCreate, _: AuthedUser, session: DbSession
) -> ExportOut:
    """Start an async filtered CSV export. Returns a job id immediately."""
    from ...export.service import create_export_job

    export = await create_export_job(session, body)
    return ExportOut(
        id=export.id, status=export.status, row_count=export.row_count,
        download_url=None, error=export.error, created_at=export.created_at,
        expires_at=export.expires_at,
    )


@router.get("/people/export/{export_id}", response_model=ExportOut)
async def export_status(
    export_id: str, _: AuthedUser, session: DbSession
) -> ExportOut:
    from ...db.enums import ExportStatus
    from ...db.models import Export

    export = await session.get(Export, export_id)
    if export is None:
        raise NotFoundError(f"No export with id {export_id!r}.")
    return ExportOut(
        id=export.id, status=export.status, row_count=export.row_count,
        download_url=(
            f"/api/v1/people/export/{export.id}/download"
            if export.status == ExportStatus.COMPLETED
            else None
        ),
        error=export.error, created_at=export.created_at, expires_at=export.expires_at,
    )


@router.get("/people/export/{export_id}/download")
async def download_export(
    export_id: str, _: AuthedUser, session: DbSession
) -> FileResponse:
    from ...db.enums import ExportStatus
    from ...db.models import Export

    export = await session.get(Export, export_id)
    if export is None:
        raise NotFoundError(f"No export with id {export_id!r}.")
    if export.status != ExportStatus.COMPLETED or not export.file_path:
        raise AppError(
            f"Export {export_id!r} is {export.status}, not ready for download.",
            code=ErrorCode.EXPORT_NOT_READY,
            details={"status": export.status},
        )
    path = absolute_path(export.file_path)
    if not path.exists():
        raise NotFoundError(
            "The export file has expired and is no longer on disk.",
        )
    return FileResponse(path, media_type="text/csv", filename=f"records-{export_id}.csv")
