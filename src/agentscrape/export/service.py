"""Asynchronous filtered CSV export.

Async because a filtered export can span the whole dataset and the client should
not hold a request open for it. The job id comes back immediately; the frontend
polls for status and then downloads.

Columns match the client's working spreadsheet:
    Hospital, Specialty, R/F, Full Name, PGY, Class of, Email
plus an optional empty "Has Been Emailed?" column the client fills in itself
(outreach state is deliberately not tracked by this backend), and optional
provenance columns.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.enums import ExportStatus
from ..db.models import Export
from ..db.repositories.query import RecordFilters, query_records
from ..db.session import session_scope
from ..domain.schemas import ExportCreate
from ..storage.artifacts import relative_path

log = logging.getLogger("agentscrape.export")

# Background tasks are kept in a module-level set: asyncio holds only a weak
# reference to a running task, so a fire-and-forget export could be garbage
# collected mid-write.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


BASE_COLUMNS = [
    "Hospital", "Specialty", "R/F", "Full Name", "PGY", "Class of", "Email",
]
STATUS_COLUMNS = ["Status", "Confidence", "First Seen", "Last Seen"]
PROVENANCE_COLUMNS = [
    "Source URL", "Page Title", "Captured At", "Extraction Method",
    "Screenshot Available", "Version",
]
EMAILED_COLUMN = "Has Been Emailed?"

PAGE_SIZE = 500


async def create_export_job(session: AsyncSession, body: ExportCreate) -> Export:
    """Persist the job and kick off generation in the background."""
    expires_at = (
        datetime.now(UTC) + timedelta(days=settings.export_retention_days)
        if settings.export_retention_days is not None
        else None
    )
    export = Export(
        status=ExportStatus.PENDING,
        filters=body.filters or {},
        include_provenance=body.include_provenance,
        include_emailed_column=body.include_emailed_column,
        expires_at=expires_at,
    )
    session.add(export)
    await session.flush()
    await session.commit()

    # The poll endpoint is how the client learns the outcome.
    _spawn(run_export(export.id))
    return export


def _filters_from_payload(payload: dict) -> RecordFilters:
    changed_since = payload.get("changed_since")
    if isinstance(changed_since, str) and changed_since:
        try:
            changed_since = datetime.fromisoformat(changed_since)
        except ValueError:
            changed_since = None
    return RecordFilters.from_query(**{**payload, "changed_since": changed_since})


async def run_export(export_id: str) -> None:
    """Generate the CSV. Failures are recorded on the job, never raised."""
    try:
        async with session_scope() as session:
            export = await session.get(Export, export_id)
            if export is None:
                return
            filters = _filters_from_payload(dict(export.filters or {}))
            include_provenance = export.include_provenance
            include_emailed = export.include_emailed_column
            await session.execute(
                update(Export)
                .where(Export.id == export_id)
                .values(status=ExportStatus.RUNNING)
            )

        settings.ensure_dirs()
        path = settings.export_dir / f"{export_id}.csv"
        rows_written = 0

        columns = list(BASE_COLUMNS)
        if include_emailed:
            columns.append(EMAILED_COLUMN)
        columns += STATUS_COLUMNS
        if include_provenance:
            columns += PROVENANCE_COLUMNS

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()

            cursor: str | None = None
            while True:
                async with session_scope() as session:
                    items, cursor, has_more = await query_records(
                        session, filters, cursor=cursor, limit=PAGE_SIZE
                    )
                    provenance = (
                        await _provenance_for(session, [i["id"] for i in items])
                        if include_provenance
                        else {}
                    )

                for item in items:
                    writer.writerow(
                        _row(item, provenance.get(item["id"]), include_emailed, include_provenance)
                    )
                    rows_written += 1
                if not has_more or not cursor:
                    break

        async with session_scope() as session:
            await session.execute(
                update(Export)
                .where(Export.id == export_id)
                .values(
                    status=ExportStatus.COMPLETED,
                    file_path=relative_path(path),
                    row_count=rows_written,
                )
            )
        log.info("export %s completed with %d rows", export_id, rows_written)

    except Exception as exc:
        log.exception("export %s failed", export_id)
        async with session_scope() as session:
            await session.execute(
                update(Export)
                .where(Export.id == export_id)
                .values(status=ExportStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
            )


def _row(
    item: dict, version, include_emailed: bool, include_provenance: bool
) -> dict[str, object]:
    row: dict[str, object] = {
        "Hospital": item.get("hospital") or "",
        "Specialty": item.get("area") or "",
        # The client's R/F column: R for resident, F for fellow, blank if unknown.
        "R/F": {"resident": "R", "fellow": "F"}.get(item.get("role") or "", ""),
        "Full Name": item.get("full_name") or "",
        "PGY": item.get("pgy") or "",
        "Class of": item.get("year") or "",
        "Email": item.get("email") or "",
    }
    if include_emailed:
        # Deliberately blank: outreach state is the client's to track.
        row[EMAILED_COLUMN] = ""
    row.update(
        {
            "Status": item.get("status") or "",
            "Confidence": item.get("confidence"),
            "First Seen": _iso(item.get("first_seen_at")),
            "Last Seen": _iso(item.get("last_seen_at")),
        }
    )
    if include_provenance:
        row.update(
            {
                "Source URL": getattr(version, "source_url", "") or "",
                "Page Title": getattr(version, "page_title", "") or "",
                "Captured At": _iso(getattr(version, "captured_at", None)),
                "Extraction Method": getattr(version, "extraction_method", "") or "",
                "Screenshot Available": getattr(version, "screenshot_available", False),
                "Version": getattr(version, "version_no", "") or "",
            }
        )
    return row


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


async def _provenance_for(session: AsyncSession, record_ids: list[str]) -> dict:
    from sqlalchemy import select

    from ..db.models import Record, RecordVersion

    if not record_ids:
        return {}
    rows = await session.execute(
        select(Record.id, RecordVersion)
        .join(RecordVersion, RecordVersion.id == Record.current_version_id)
        .where(Record.id.in_(record_ids))
    )
    return dict(rows.all())
