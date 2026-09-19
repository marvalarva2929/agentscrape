"""Site and known-path persistence."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...urls import canonicalize, entry_url, host_of, url_hash
from ..enums import ValidationStatus
from ..models import KnownPath, Site

log = logging.getLogger("agentscrape.sites")

# A path that fails this many runs in a row has stopped working and is retired.
MAX_CONSECUTIVE_FAILURES = 3


async def get_site_by_domain(session: AsyncSession, root_domain: str) -> Site | None:
    return await session.scalar(
        select(Site).where(Site.root_domain == root_domain.lower())
    )


async def get_site(session: AsyncSession, site_id: str) -> Site | None:
    return await session.get(Site, site_id)


async def upsert_site(session: AsyncSession, url: str) -> Site:
    """Find or create the Site for a URL. Sites persist across runs."""
    canonical = canonicalize(url) or url
    host = host_of(canonical)
    if not host:
        raise ValueError(f"could not parse a hostname from {url!r}")

    site = await get_site_by_domain(session, host)
    if site is None:
        site = Site(
            root_domain=host,
            canonical_url=entry_url(canonical),
            validation_status=ValidationStatus.PENDING,
        )
        session.add(site)
        await session.flush()
        log.info("registered new site %s (%s)", site.id, host)
    return site


async def record_validation(
    session: AsyncSession,
    site: Site,
    *,
    status: ValidationStatus,
    reason: str,
    institution_type: str | None = None,
    hospital_name: str | None = None,
) -> None:
    site.validation_status = status
    site.validation_reason = reason
    if institution_type:
        site.institution_type = institution_type
    if hospital_name and not site.hospital_name:
        site.hospital_name = hospital_name


async def active_known_paths(
    session: AsyncSession, site_id: str, *, limit: int = 50
) -> list[KnownPath]:
    """Known-good paths, best first. These jump the queue on the next run."""
    rows = await session.execute(
        select(KnownPath)
        .where(KnownPath.site_id == site_id, KnownPath.is_active.is_(True))
        .order_by(KnownPath.score.desc(), KnownPath.last_success_at.desc().nullslast())
        .limit(limit)
    )
    return list(rows.scalars().all())


def compute_path_score(
    *, success_count: int, failure_count: int, avg_records: float,
    last_success_at: datetime | None, now: datetime | None = None,
) -> float:
    """Priority for a known path: proven yield, decayed by staleness.

    A path that stops producing decays out of the front of the queue rather than
    being dropped immediately, because one bad run is often a transient error.
    """
    now = now or datetime.now(UTC)
    attempts = success_count + failure_count
    if attempts == 0:
        return 0.0
    hit_rate = success_count / attempts
    yield_factor = min(avg_records, 50.0) / 10.0  # cap so one huge page can't dominate

    staleness = 1.0
    if last_success_at is not None:
        last = last_success_at if last_success_at.tzinfo else last_success_at.replace(tzinfo=UTC)
        days = max((now - last).days, 0)
        staleness = 0.5 ** (days / 180)  # half-life of six months

    return round(hit_rate * (1.0 + yield_factor) * staleness * 10.0, 3)


async def record_path_outcome(
    session: AsyncSession,
    *,
    site_id: str,
    url: str,
    records_found: int,
    content_hash: str | None = None,
    now: datetime | None = None,
) -> None:
    """Update a known path's statistics after visiting it.

    Called for every visited URL, not only successful ones: a path that yields
    nothing needs its failure counted so it can decay.
    """
    now = now or datetime.now(UTC)
    canonical = canonicalize(url) or url
    digest = url_hash(canonical)
    succeeded = records_found > 0

    existing = await session.scalar(
        select(KnownPath).where(
            KnownPath.site_id == site_id, KnownPath.url_hash == digest
        )
    )

    if existing is None:
        if not succeeded:
            return  # never promote a page that produced nothing
        path = KnownPath(
            site_id=site_id, url=canonical, url_hash=digest,
            success_count=1, failure_count=0, consecutive_failures=0,
            last_success_at=now, last_attempt_at=now,
            avg_records=float(records_found), last_content_hash=content_hash,
            is_active=True,
        )
        path.score = compute_path_score(
            success_count=1, failure_count=0, avg_records=float(records_found),
            last_success_at=now, now=now,
        )
        session.add(path)
        return

    existing.last_attempt_at = now
    if succeeded:
        existing.success_count += 1
        existing.consecutive_failures = 0
        existing.last_success_at = now
        # Running mean of yield across successful visits.
        existing.avg_records = (
            (existing.avg_records * (existing.success_count - 1) + records_found)
            / existing.success_count
        )
        existing.is_active = True
    else:
        existing.failure_count += 1
        existing.consecutive_failures += 1
        if existing.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            existing.is_active = False
            log.info(
                "retiring known path %s after %d consecutive failures",
                existing.url, existing.consecutive_failures,
            )
    if content_hash:
        existing.last_content_hash = content_hash

    existing.score = compute_path_score(
        success_count=existing.success_count,
        failure_count=existing.failure_count,
        avg_records=existing.avg_records,
        last_success_at=existing.last_success_at,
        now=now,
    )


async def delete_known_path(session: AsyncSession, site_id: str, path_id: str) -> bool:
    """Admin operation: clear a stale path manually."""
    result = await session.execute(
        delete(KnownPath).where(KnownPath.id == path_id, KnownPath.site_id == site_id)
    )
    return bool(result.rowcount)


async def save_fingerprint(
    session: AsyncSession, site_id: str, fingerprint: dict[str, str]
) -> None:
    await session.execute(
        update(Site)
        .where(Site.id == site_id)
        .values(last_fingerprint=fingerprint, last_scraped_at=datetime.now(UTC))
    )


async def set_dominant_specialty(
    session: AsyncSession, site_id: str, specialty: str | None
) -> None:
    if specialty:
        await session.execute(
            update(Site).where(Site.id == site_id).values(dominant_specialty=specialty)
        )


async def record_visit(
    session: AsyncSession,
    *,
    site_run_id: str,
    url: str,
    fetch_mode: str | None = None,
    http_status: int | None = None,
    content_hash: str | None = None,
    records_yielded: int = 0,
    error: str | None = None,
) -> None:
    """Append to the per-SiteRun visited set.

    ON CONFLICT DO NOTHING because the same URL can be reached by two entry
    points concurrently; the first writer wins and the second is a no-op.
    """
    from ..models import SiteRunVisit

    canonical = canonicalize(url) or url
    await session.execute(
        pg_insert(SiteRunVisit)
        .values(
            site_run_id=site_run_id, url=canonical, url_hash=url_hash(canonical),
            fetch_mode=fetch_mode, http_status=http_status,
            content_hash=content_hash, records_yielded=records_yielded, error=error,
        )
        .on_conflict_do_nothing(index_elements=["site_run_id", "url_hash"])
    )


async def visited_hashes(session: AsyncSession, site_run_id: str) -> set[str]:
    """URLs already handled in this SiteRun — the resume anchor."""
    from ..models import SiteRunVisit

    rows = await session.execute(
        select(SiteRunVisit.url_hash).where(SiteRunVisit.site_run_id == site_run_id)
    )
    return set(rows.scalars().all())


async def claim_url(session: AsyncSession, site_run_id: str, url: str) -> bool:
    """Atomically reserve a URL for this SiteRun.

    Returns False when another concurrent fetch already claimed it. This is what
    stops multiple entry points inside one site from re-fetching the same page.
    """
    from ..models import SiteRunVisit

    canonical = canonicalize(url) or url
    result = await session.execute(
        pg_insert(SiteRunVisit)
        .values(site_run_id=site_run_id, url=canonical, url_hash=url_hash(canonical))
        .on_conflict_do_nothing(index_elements=["site_run_id", "url_hash"])
    )
    return bool(result.rowcount)


async def update_visit(
    session: AsyncSession,
    *,
    site_run_id: str,
    url: str,
    fetch_mode: str | None = None,
    http_status: int | None = None,
    content_hash: str | None = None,
    records_yielded: int = 0,
    error: str | None = None,
) -> None:
    """Fill in a claimed visit once the fetch completes."""
    from ..models import SiteRunVisit

    canonical = canonicalize(url) or url
    await session.execute(
        update(SiteRunVisit)
        .where(
            SiteRunVisit.site_run_id == site_run_id,
            SiteRunVisit.url_hash == url_hash(canonical),
        )
        .values(
            fetch_mode=fetch_mode, http_status=http_status,
            content_hash=content_hash, records_yielded=records_yielded, error=error,
        )
    )
