"""The work queue.

`site_runs` is the queue: a worker claims the next pending row with
`FOR UPDATE SKIP LOCKED`, which gives atomic hand-out across workers without a
separate broker and survives a restart, because the claim is a row state rather
than in-memory.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.enums import SiteRunStatus
from ..db.models import Site, SiteRun

log = logging.getLogger("agentscrape.queue")

# A running SiteRun whose worker died leaves a stale claim; reclaim after this.
STALE_CLAIM_MINUTES = 30


async def claim_next_site(
    session: AsyncSession, run_id: str, agent_id: str
) -> tuple[str, str, str, bool, int] | None:
    """Claim one pending site. Returns (site_run_id, site_id, url, force, budget)."""
    row = (
        await session.execute(
            select(SiteRun, Site.canonical_url)
            .join(Site, Site.id == SiteRun.site_id)
            .where(SiteRun.run_id == run_id, SiteRun.status == SiteRunStatus.PENDING)
            .order_by(SiteRun.position, SiteRun.created_at, SiteRun.id)
            .limit(1)
            .with_for_update(of=SiteRun, skip_locked=True)
        )
    ).first()

    if row is None:
        return None

    site_run, canonical_url = row
    await session.execute(
        update(SiteRun)
        .where(SiteRun.id == site_run.id)
        .values(
            status=SiteRunStatus.RUNNING,
            agent_id=agent_id,
            started_at=datetime.now(UTC),
            heartbeat_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return (
        site_run.id, site_run.site_id, canonical_url,
        site_run.force_rescan, site_run.step_budget,
    )


async def reclaim_stale(session: AsyncSession, run_id: str) -> int:
    """Return sites abandoned by a dead worker to the queue.

    The server is stopped on demand, so a resumed run always finds rows left in
    `running`. Completed sites are untouched, which is what makes resume cheap.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=STALE_CLAIM_MINUTES)
    result = await session.execute(
        update(SiteRun)
        .where(
            SiteRun.run_id == run_id,
            SiteRun.status == SiteRunStatus.RUNNING,
            (SiteRun.heartbeat_at.is_(None)) | (SiteRun.heartbeat_at < cutoff),
        )
        .values(status=SiteRunStatus.PENDING, agent_id=None)
    )
    await session.commit()
    if result.rowcount:
        log.info("reclaimed %d stale site runs for run %s", result.rowcount, run_id)
    return int(result.rowcount or 0)


async def reset_running_for_resume(session: AsyncSession, run_id: str) -> int:
    """On resume, every still-`running` site goes back to pending.

    Their visited sets persist, so re-running one picks up where it left off
    rather than starting over.
    """
    result = await session.execute(
        update(SiteRun)
        .where(SiteRun.run_id == run_id, SiteRun.status == SiteRunStatus.RUNNING)
        .values(status=SiteRunStatus.PENDING, agent_id=None)
    )
    await session.commit()
    return int(result.rowcount or 0)


async def heartbeat(session: AsyncSession, site_run_id: str) -> None:
    await session.execute(
        update(SiteRun)
        .where(SiteRun.id == site_run_id)
        .values(heartbeat_at=datetime.now(UTC))
    )
    await session.commit()


async def pending_count(session: AsyncSession, run_id: str) -> int:
    from sqlalchemy import func

    return int(
        await session.scalar(
            select(func.count(SiteRun.id)).where(
                SiteRun.run_id == run_id, SiteRun.status == SiteRunStatus.PENDING
            )
        ) or 0
    )


async def cancel_pending(session: AsyncSession, run_id: str) -> int:
    """Cancellation stops new work immediately; in-flight sites finish their step."""
    result = await session.execute(
        update(SiteRun)
        .where(SiteRun.run_id == run_id, SiteRun.status == SiteRunStatus.PENDING)
        .values(status=SiteRunStatus.CANCELLED, finished_at=datetime.now(UTC))
    )
    await session.commit()
    return int(result.rowcount or 0)
