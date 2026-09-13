"""Run management: validate, create, list, detail, per-site status, SSE, cancel, retry."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from ...config import settings
from ...db.enums import TERMINAL_RUN_STATUSES, RunStatus, SiteRunStatus
from ...db.models import Run, Site, SiteRun
from ...domain.schemas import (
    Page,
    RunCreate,
    RunOut,
    SiteRunOut,
    ValidatePreview,
)
from ...orchestrator import service
from ...orchestrator.limits import MemoryCeilingExceeded
from ..deps import AuthedUser, DbSession
from ..errors import AppError, ErrorCode, NotFoundError, ResourceLimitError
from ..pagination import Cursor, clamp_limit
from ..sse import event_stream

router = APIRouter(prefix="/runs", tags=["runs"])


def _run_out(run: Run, pending: int = 0) -> RunOut:
    config = dict(run.config or {})
    return RunOut(
        id=run.id, status=run.status, label=run.label, config=config,
        stop_reason=run.stop_reason, error_message=run.error_message,
        sites_total=run.sites_total, sites_completed=run.sites_completed,
        sites_skipped=run.sites_skipped, sites_failed=run.sites_failed,
        sites_rejected=run.sites_rejected, sites_pending=pending,
        records_found=run.records_found, records_new=run.records_new,
        records_changed=run.records_changed, records_missing=run.records_missing,
        tokens_in=run.tokens_in, tokens_out=run.tokens_out,
        spend_usd=float(run.spend_usd or 0),
        max_records=config.get("max_records"),
        max_spend_usd=config.get("max_spend_usd"),
        created_at=run.created_at, started_at=run.started_at,
        finished_at=run.finished_at,
    )


@router.post("/validate", response_model=ValidatePreview)
async def validate_csv(
    _: AuthedUser,
    session: DbSession,
    file: Annotated[UploadFile, File(description="CSV of institution URLs")],
    skip_threshold: float = Query(default=None),
) -> ValidatePreview:
    """Parse and preview a CSV without creating a run.

    Shows, per row: whether the URL is valid, whether the site is known, when it
    was last scraped, how many known paths and prior records it has, and whether
    it is predicted to be skipped — so compute can be reviewed before it is spent.
    """
    content = await file.read()
    entries = service.parse_csv(content)
    if not entries:
        raise AppError(
            "No usable rows found in the uploaded CSV.",
            code=ErrorCode.INVALID_CSV,
            details={"filename": file.filename},
        )
    return await service.preview_csv(
        session,
        entries,
        skip_threshold=(
            skip_threshold if skip_threshold is not None else settings.default_skip_threshold
        ),
    )


@router.post("", response_model=RunOut, status_code=201)
async def create_run(body: RunCreate, _: AuthedUser, session: DbSession) -> RunOut:
    """Create and start a run."""
    try:
        run = await service.create_run(session, body)
    except MemoryCeilingExceeded as exc:
        raise ResourceLimitError(str(exc), details={"concurrency": body.config.concurrency}) from exc

    if run.sites_total == 0:
        raise AppError(
            "None of the supplied sites could be parsed into a hostname.",
            code=ErrorCode.INVALID_CSV,
        )

    await service.launch_run(run.id)
    await session.refresh(run)
    return _run_out(run, pending=run.sites_total)


@router.get("", response_model=Page[RunOut])
async def list_runs(
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 25,
    status: Annotated[list[str] | None, Query()] = None,
) -> Page[RunOut]:
    statement = select(Run)
    if status:
        statement = statement.where(Run.status.in_(status))

    decoded = Cursor.decode(cursor)
    if decoded is not None:
        statement = statement.where(
            (Run.created_at < decoded.sort_value)
            | ((Run.created_at == decoded.sort_value) & (Run.id < decoded.id))
        )

    limit = clamp_limit(limit)
    rows = (
        await session.execute(
            statement.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = list(rows[:limit])

    next_cursor = (
        Cursor(sort_value=rows[-1].created_at.isoformat(), id=rows[-1].id).encode()
        if has_more and rows
        else None
    )
    return Page[RunOut](
        items=[_run_out(r) for r in rows], next_cursor=next_cursor, has_more=has_more
    )


async def _get_run(session, run_id: str) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise NotFoundError(f"No run with id {run_id!r}.")
    return run


@router.get("/{run_id}", response_model=RunOut)
async def run_detail(run_id: str, _: AuthedUser, session: DbSession) -> RunOut:
    run = await _get_run(session, run_id)
    pending = int(
        await session.scalar(
            select(func.count(SiteRun.id)).where(
                SiteRun.run_id == run_id, SiteRun.status == SiteRunStatus.PENDING
            )
        ) or 0
    )
    return _run_out(run, pending=pending)


@router.get("/{run_id}/sites", response_model=Page[SiteRunOut])
async def run_sites(
    run_id: str,
    _: AuthedUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = 200,
    status: Annotated[list[str] | None, Query()] = None,
) -> Page[SiteRunOut]:
    """Per-site status snapshot. This is the SSE resync path: it returns the same
    state the stream reports, so a reconnecting client needs exactly one call."""
    await _get_run(session, run_id)

    statement = (
        select(SiteRun, Site.root_domain, Site.hospital_name)
        .join(Site, Site.id == SiteRun.site_id)
        .where(SiteRun.run_id == run_id)
    )
    if status:
        statement = statement.where(SiteRun.status.in_(status))

    decoded = Cursor.decode(cursor)
    if decoded is not None:
        statement = statement.where(SiteRun.id > decoded.id)

    limit = clamp_limit(limit)
    rows = (await session.execute(statement.order_by(SiteRun.id).limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [
        SiteRunOut.model_validate(site_run).model_copy(
            update={"domain": domain, "hospital": hospital}
        )
        for site_run, domain, hospital in rows
    ]
    next_cursor = (
        Cursor(sort_value=rows[-1][0].id, id=rows[-1][0].id).encode()
        if has_more and rows
        else None
    )
    return Page[SiteRunOut](items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/{run_id}/events")
async def run_events(run_id: str, _: AuthedUser, session: DbSession) -> StreamingResponse:
    """SSE progress stream.

    EventSource cannot set an Authorization header, so this endpoint also accepts
    the token as `?token=` (see `require_auth`). It is the only one that does.
    """
    await _get_run(session, run_id)
    return StreamingResponse(
        event_stream(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: str, _: AuthedUser, session: DbSession) -> RunOut:
    run = await _get_run(session, run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        raise AppError(
            f"Run is already {run.status} and cannot be cancelled.",
            code=ErrorCode.RUN_NOT_CANCELLABLE,
            status_code=409,
            details={"status": run.status},
        )
    await service.cancel_run(session, run_id)
    await session.refresh(run)
    return _run_out(run)


@router.post("/{run_id}/sites/{site_id}/retry", response_model=SiteRunOut)
async def retry_site(
    run_id: str, site_id: str, _: AuthedUser, session: DbSession
) -> SiteRunOut:
    """Retry one failed or skipped site. Always forces a rescan."""
    run = await _get_run(session, run_id)
    try:
        site_run = await service.retry_site(session, run_id, site_id)
    except LookupError as exc:
        raise NotFoundError(f"Site {site_id!r} is not part of run {run_id!r}.") from exc
    except ValueError as exc:
        raise AppError(
            str(exc), code=ErrorCode.SITE_NOT_RETRYABLE, status_code=409
        ) from exc

    # A finished run needs restarting for the retried site to be picked up.
    from ...orchestrator.pool import get_active

    if get_active(run_id) is None:
        if run.status in TERMINAL_RUN_STATUSES:
            run.status = RunStatus.PENDING
            run.finished_at = None
            await session.commit()
        await service.launch_run(run_id)

    site = await session.get(Site, site_id)
    return SiteRunOut.model_validate(site_run).model_copy(
        update={
            "domain": site.root_domain if site else None,
            "hospital": site.hospital_name if site else None,
        }
    )
