"""Client CSV requests, and the staff queue that runs them.

Clients cannot start crawls: billing is per school, so they submit a CSV of the
schools they want and staff review and launch it. The client half is one POST;
everything else lives behind the admin password.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from sqlalchemy import select

from ...config import settings
from ...db.enums import SubmissionStatus
from ...db.models import CsvSubmission
from ...domain.schemas import (
    Page,
    RunConfigIn,
    RunCreate,
    SubmissionOut,
    SubmissionRunRequest,
)
from ...orchestrator import service
from ..deps import AdminUser, AuthedUser, DbSession
from ..errors import AppError, ErrorCode, NotFoundError
from ..pagination import Cursor, clamp_limit

router = APIRouter(tags=["submissions"])


def _out(submission: CsvSubmission) -> SubmissionOut:
    return SubmissionOut(
        id=submission.id,
        filename=submission.filename,
        note=submission.note,
        status=submission.status,
        row_count=submission.row_count,
        valid_count=submission.valid_count,
        rows=submission.rows or [],
        run_id=submission.run_id,
        created_at=submission.created_at,
        reviewed_at=submission.reviewed_at,
    )


@router.post("/submissions", response_model=SubmissionOut, status_code=201)
async def submit_csv(
    scope: AuthedUser,
    session: DbSession,
    file: Annotated[UploadFile, File(description="CSV of requested schools")],
    note: Annotated[str | None, Form()] = None,
) -> SubmissionOut:
    """Submit a CSV of schools to be crawled. Does not start anything."""
    entries = service.parse_csv(await file.read())
    if not entries:
        raise AppError(
            "No usable rows found in the uploaded CSV.",
            code=ErrorCode.INVALID_CSV,
            details={"filename": file.filename},
        )

    preview = await service.preview_csv(
        session, entries, skip_threshold=settings.default_skip_threshold
    )
    submission = CsvSubmission(
        filename=file.filename,
        note=note,
        status=SubmissionStatus.PENDING,
        rows=[row.model_dump(mode="json") for row in preview.rows],
        row_count=preview.total_rows,
        valid_count=preview.valid_rows,
        submitted_by=scope,
    )
    session.add(submission)
    await session.flush()
    await session.commit()
    return _out(submission)


@router.get("/admin/submissions", response_model=Page[SubmissionOut])
async def list_submissions(
    _: AdminUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 50,
    status: Annotated[list[str] | None, Query()] = None,
) -> Page[SubmissionOut]:
    """The staff queue: what clients have asked for and what has been run."""
    statement = select(CsvSubmission)
    if status:
        statement = statement.where(CsvSubmission.status.in_(status))

    decoded = Cursor.decode(cursor)
    if decoded is not None:
        statement = statement.where(CsvSubmission.id < decoded.id)

    limit = clamp_limit(limit)
    rows = (
        await session.execute(
            statement.order_by(CsvSubmission.created_at.desc(), CsvSubmission.id.desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = list(rows[:limit])

    return Page[SubmissionOut](
        items=[_out(r) for r in rows],
        next_cursor=(
            Cursor(sort_value=rows[-1].id, id=rows[-1].id).encode()
            if has_more and rows
            else None
        ),
        has_more=has_more,
    )


@router.get("/admin/submissions/{submission_id}", response_model=SubmissionOut)
async def submission_detail(
    submission_id: str, _: AdminUser, session: DbSession
) -> SubmissionOut:
    submission = await session.get(CsvSubmission, submission_id)
    if submission is None:
        raise NotFoundError(f"No submission with id {submission_id!r}.")
    return _out(submission)


@router.post("/admin/submissions/{submission_id}/run", response_model=SubmissionOut)
async def run_submission(
    submission_id: str,
    body: SubmissionRunRequest,
    _: AdminUser,
    session: DbSession,
) -> SubmissionOut:
    """Launch a crawl for every valid school in a submission."""
    submission = await session.get(CsvSubmission, submission_id)
    if submission is None:
        raise NotFoundError(f"No submission with id {submission_id!r}.")
    if submission.status == SubmissionStatus.RUNNING:
        raise AppError(
            "This submission is already running.",
            code=ErrorCode.CONFLICT,
            status_code=409,
        )

    urls = [row["url"] for row in (submission.rows or []) if row.get("valid") and row.get("url")]
    if not urls:
        raise AppError(
            "This submission has no valid schools to crawl.",
            code=ErrorCode.INVALID_CSV,
            status_code=400,
        )

    run = await service.create_run(
        session,
        RunCreate(
            sites=urls,
            config=RunConfigIn(
                concurrency=body.concurrency,
                max_spend_usd=body.max_spend_usd,
                force_rescan=body.force_rescan,
                label=f"submission:{submission.filename or submission.id}",
            ),
        ),
    )
    await service.launch_run(run.id)

    submission.status = SubmissionStatus.RUNNING
    submission.run_id = run.id
    submission.reviewed_at = datetime.now(UTC)
    await session.commit()
    return _out(submission)
