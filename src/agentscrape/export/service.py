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


# The client's working sheet, plus Position now that everyone on a site is
# collected rather than just trainees. Provenance columns are deliberately not
# exported: the source URL is for reviewing a person in the app, not for the
# outreach sheet.
BASE_COLUMNS = [
    "Hospital", "Program", "Specialty", "R/F", "Position", "Full Name",
    "PGY", "Class of", "Email",
]
STATUS_COLUMNS = ["Status", "Last Seen"]

# R for resident, F for fellow; anyone else is labelled by what they are.
_RF = {"resident": "R", "fellow": "F"}

PAGE_SIZE = 500


async def create_export_job(session: AsyncSession, body: ExportCreate) -> Export:
    """Persist the job and kick off generation in the background.

    Exports are normally scoped to one programme (`filters={"program_id": ...}`),
    which is how the UI offers them.
    """
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
            await session.execute(
                update(Export)
                .where(Export.id == export_id)
                .values(status=ExportStatus.RUNNING)
            )

        settings.ensure_dirs()
        path = settings.export_dir / f"{export_id}.csv"
        rows_written = 0

        columns = [*BASE_COLUMNS, *STATUS_COLUMNS]

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()

            cursor: str | None = None
            while True:
                async with session_scope() as session:
                    items, cursor, has_more = await query_records(
                        session, filters, cursor=cursor, limit=PAGE_SIZE
                    )

                for item in items:
                    writer.writerow(_row(item))
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


def _row(item: dict) -> dict[str, object]:
    category = str(item.get("category") or "")
    return {
        "Hospital": item.get("hospital") or "",
        "Program": item.get("program_name") or "",
        "Specialty": item.get("area") or "",
        # R/F for trainees; anyone else is labelled by category.
        "R/F": _RF.get(category, category.title() if category != "unknown" else ""),
        "Position": item.get("position") or "",
        "Full Name": item.get("full_name") or "",
        # Exactly as the page printed it; blank when it did not say.
        "PGY": item.get("pgy") or "",
        "Class of": item.get("year") or "",
        "Email": item.get("email") or "",
        "Status": item.get("status") or "",
        "Last Seen": _iso(item.get("last_seen_at")),
    }


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""
