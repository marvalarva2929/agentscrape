"""Screenshots on local disk with a configurable retention window.

Provenance must survive screenshot expiry: when the sweeper deletes an image it
clears `screenshot_path` and flips `screenshot_available`, but the URL, title,
timestamp, method and field locations on the RecordVersion are untouched.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from ..config import settings
from ..db.models import Export, RecordVersion
from ..db.session import session_scope

log = logging.getLogger("agentscrape.artifacts")


def screenshot_expiry(captured_at: datetime | None = None) -> datetime | None:
    """None means keep forever, which is a valid configuration."""
    if settings.screenshot_retention_days is None:
        return None
    base = captured_at or datetime.now(UTC)
    return base + timedelta(days=settings.screenshot_retention_days)


def save_screenshot(image: bytes, *, site_run_id: str, url_hash: str) -> Path:
    """Write a screenshot under artifacts/screenshots/<site_run>/<url_hash>.png."""
    directory = settings.screenshot_dir / site_run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{url_hash[:32]}.png"
    path.write_bytes(image)
    return path


def relative_path(path: Path) -> str:
    """Store paths relative to ARTIFACT_DIR so the directory can be relocated."""
    try:
        return str(path.resolve().relative_to(settings.artifact_dir.resolve()))
    except ValueError:
        return str(path)


def absolute_path(stored: str) -> Path:
    candidate = Path(stored)
    return candidate if candidate.is_absolute() else settings.artifact_dir / stored


async def sweep_expired_screenshots(*, now: datetime | None = None) -> dict[str, int]:
    """Delete expired screenshots, keeping their provenance rows intact.

    Runs on API startup and from the CLI rather than cron, because the box is
    stopped when idle and a cron schedule would silently never fire.
    """
    now = now or datetime.now(UTC)
    deleted = errors = 0

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(RecordVersion.id, RecordVersion.screenshot_path).where(
                    RecordVersion.screenshot_available.is_(True),
                    RecordVersion.screenshot_expires_at.isnot(None),
                    RecordVersion.screenshot_expires_at <= now,
                )
            )
        ).all()

        for version_id, stored in rows:
            if stored:
                try:
                    absolute_path(stored).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not delete %s: %s", stored, exc)
                    errors += 1
            await session.execute(
                update(RecordVersion)
                .where(RecordVersion.id == version_id)
                .values(screenshot_available=False, screenshot_path=None)
            )
            deleted += 1

    if deleted:
        log.info("screenshot sweep: expired %d (errors %d)", deleted, errors)
    return {"expired": deleted, "errors": errors}


async def sweep_expired_exports(*, now: datetime | None = None) -> dict[str, int]:
    """Delete generated CSVs past their window and mark the jobs expired."""
    from ..db.enums import ExportStatus

    now = now or datetime.now(UTC)
    removed = 0
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Export.id, Export.file_path).where(
                    Export.status == ExportStatus.COMPLETED,
                    Export.expires_at.isnot(None),
                    Export.expires_at <= now,
                )
            )
        ).all()
        for export_id, file_path in rows:
            if file_path:
                try:
                    Path(file_path).unlink(missing_ok=True)
                except OSError:
                    pass
            await session.execute(
                update(Export)
                .where(Export.id == export_id)
                .values(status=ExportStatus.EXPIRED, file_path=None)
            )
            removed += 1
    if removed:
        log.info("export sweep: expired %d", removed)
    return {"expired": removed}


def orphaned_screenshot_dirs() -> list[Path]:
    """Screenshot directories with no matching SiteRun directory contents.

    Reported by the admin stats endpoint rather than deleted automatically.
    """
    root = settings.screenshot_dir
    if not root.exists():
        return []
    return [d for d in root.iterdir() if d.is_dir() and not any(d.iterdir())]
