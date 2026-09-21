"""Run management: create, list, detail, per-site status, SSE, cancel, retry."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from ...db.enums import TERMINAL_RUN_STATUSES, RunStatus, SiteRunStatus
from ...db.models import Run, Site, SiteRun
from ...domain.schemas import (
    Page,
    QueueOut,
    RunCreate,
    RunOut,
    SiteRunOut,
)
from ...orchestrator import scheduler, service
from ...orchestrator.limits import MemoryCeilingExceeded
from ..deps import AuthedUser, DbSession
from ..errors import AppError, ErrorCode, NotFoundError, ResourceLimitError
from ..pagination import Cursor, clamp_limit
from ..sse import event_stream

router = APIRouter(prefix="/runs", tags=["runs"])


def _run_out(run: Run, pending: int = 0, queue_position: int | None = None) -> RunOut:
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
        max_trainees=config.get("max_trainees"),
        max_emails=config.get("max_emails"),
        max_spend_usd=config.get("max_spend_usd"),
        created_at=run.created_at, started_at=run.started_at,
        finished_at=run.finished_at,
        queued=scheduler.is_queued(config), queue_position=queue_position,
    )


@router.post("", response_model=RunOut, status_code=201)
async def create_run(body: RunCreate, _: AuthedUser, session: DbSession) -> RunOut:
    """Create and start a run: a crawl, a directory search, or both. Open to
    clients as well as staff; `config.max_spend_usd` is what bounds spend."""
    try:
        run = await service.create_run(session, body)
    except MemoryCeilingExceeded as exc:
        raise ResourceLimitError(str(exc), details={"concurrency": body.config.concurrency}) from exc
    except service.DirectorySearchUnavailable as exc:
        raise AppError(
            str(exc), code=ErrorCode.DIRECTORY_SEARCH_UNAVAILABLE, status_code=409,
            details={"sites": exc.problems},
        ) from exc

    if run.sites_total == 0:
        raise AppError(
            "None of the supplied sites could be parsed into a hostname.",
            code=ErrorCode.INVALID_CSV,
        )

    if body.config.queued:
        # Starts now only if the process is idle; otherwise it waits, and the
        # run that finishes last will start it.
        await scheduler.start_next_if_idle()
    else:
        await service.launch_run(run.id)

    await session.refresh(run)
    position = await scheduler.queue_position(session, run.id)
    return _run_out(run, pending=run.sites_total, queue_position=position)


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
        # The cursor carries the timestamp as ISO text; Postgres will not
        # compare text with a timestamptz, so page two used to fail with a 500.
        try:
            after = datetime.fromisoformat(str(decoded.sort_value))
        except ValueError as exc:
            raise AppError("Invalid cursor.", code=ErrorCode.INVALID_CURSOR) from exc
        statement = statement.where(
            (Run.created_at < after)
            | ((Run.created_at == after) & (Run.id < decoded.id))
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


@router.get("/queue", response_model=QueueOut)
async def run_queue(_: AuthedUser, session: DbSession) -> QueueOut:
    """The whole queue: what holds the model budget now, what is waiting and in
    what order, and how far each school in each run has got.

    Declared before `/{run_id}` so the literal path wins the match.
    """
    return await scheduler.queue_view(session)


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
    position = await scheduler.queue_position(session, run_id)
    return _run_out(run, pending=pending, queue_position=position)


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
        # Stopping something already stopped is what the caller wanted, so it
        # succeeds rather than raising. Refusing it meant a run that finished
        # between drawing the stop button and pressing it reported that the
        # crawl could not be stopped, which reads as a failure to stop it.
        return _run_out(run)
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
        if scheduler.is_queued(run.config):
            await scheduler.start_next_if_idle()
        else:
            await service.launch_run(run_id)

    site = await session.get(Site, site_id)
    return SiteRunOut.model_validate(site_run).model_copy(
        update={
            "domain": site.root_domain if site else None,
            "hospital": site.hospital_name if site else None,
        }
    )
