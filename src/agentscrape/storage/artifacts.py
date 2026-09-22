"""Artifact paths and cleanup. Screenshot persistence is disabled.

Provenance must survive screenshot expiry: when the sweeper deletes an image it
clears `screenshot_path` and flips `screenshot_available`, but the URL, title,
timestamp, method and field locations on the RecordVersion are untouched.
"""

from __future__ import annotations

import logging
import os
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


def png_dimensions(image: bytes) -> tuple[int | None, int | None]:
    """Width and height straight from the PNG IHDR chunk.

    Avoids pulling in an image library for eight bytes of header. Returns
    (None, None) for anything that is not a PNG.
    """
    if len(image) < 24 or image[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None
    width = int.from_bytes(image[16:20], "big")
    height = int.from_bytes(image[20:24], "big")
    return (width or None), (height or None)


def save_screenshot(image: bytes, *, site_run_id: str, url_hash: str) -> None:
    """Screenshot persistence is disabled; vision images stay in memory only."""
    return None


async def replace_school_screenshots(site_id: str, keep_site_run_id: str) -> dict[str, int]:
    """Drop a school's older screenshots once it has been crawled again.

    Product decision: a school keeps the screenshots from its most recent run
    only. Provenance rows survive — URL, title, timestamp, method and field
    boxes stay, and `screenshot_available` flips to false for the older ones.
    """
    from sqlalchemy import select, update

    from ..db.models import Record, RecordVersion

    removed = 0
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(RecordVersion.id, RecordVersion.screenshot_path)
                .join(Record, Record.id == RecordVersion.record_id)
                .where(
                    Record.site_id == site_id,
                    RecordVersion.screenshot_available.is_(True),
                    RecordVersion.site_run_id != keep_site_run_id,
                )
            )
        ).all()

        for version_id, stored in rows:
            if stored:
                try:
                    absolute_path(stored).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not delete %s: %s", stored, exc)
            await session.execute(
                update(RecordVersion)
                .where(RecordVersion.id == version_id)
                .values(screenshot_available=False, screenshot_path=None)
            )
            removed += 1

    # Sweep now-empty run directories so the artifact tree does not accumulate.
    root = settings.screenshot_dir
    if root.exists():
        for directory in root.iterdir():
            if directory.is_dir() and directory.name != keep_site_run_id:
                try:
                    if not any(directory.iterdir()):
                        directory.rmdir()
                except OSError:
                    pass

    if removed:
        log.info("replaced %d older screenshots for site %s", removed, site_id)
    return {"replaced": removed}


def relative_path(path: Path | None) -> str | None:
    """Store paths relative to ARTIFACT_DIR so the directory can be relocated."""
    if path is None:
        return None
    # Legacy callers (including demo seeding) may write before asking for a
    # stored path. Discard those screenshots too; export paths are unaffected.
    if path.resolve().is_relative_to(settings.screenshot_dir.resolve()):
        path.unlink(missing_ok=True)
        return None
    try:
        return str(path.resolve().relative_to(settings.artifact_dir.resolve()))
    except ValueError:
        return str(path)


def absolute_path(stored: str) -> Path:
    candidate = Path(stored)
    return candidate if candidate.is_absolute() else settings.artifact_dir / stored


async def sweep_expired_screenshots(*, now: datetime | None = None) -> dict[str, int]:
    """Remove all stored screenshots, regardless of their former retention window.

    Runs on API startup and from the CLI rather than cron, because the box is
    stopped when idle and a cron schedule would silently never fire.
    """
    deleted = errors = 0
    # Enumerate the dedicated screenshot tree, including files with no DB row.
    # Never follow links or use database paths as deletion targets.
    root = settings.screenshot_dir
    artifact_root = settings.artifact_dir.resolve()
    if root.is_symlink() or root.resolve().parent != artifact_root:
        raise ValueError("Screenshot directory must be directly inside ARTIFACT_DIR")

    def report_error(exc: OSError) -> None:
        nonlocal errors
        errors += 1
        log.warning("could not remove screenshot: %s", exc)

    for directory, subdirs, files in os.walk(root, topdown=False, followlinks=False,
                                            onerror=report_error):
        for name in files:
            try:
                (Path(directory) / name).unlink(missing_ok=True)
                deleted += 1
            except OSError as exc:
                report_error(exc)
        for name in subdirs:
            child = Path(directory) / name
            try:
                if child.is_symlink():
                    child.unlink()
                else:
                    child.rmdir()
            except OSError as exc:
                report_error(exc)

    # Keep people and their source provenance; only clear screenshot metadata.
    async with session_scope() as session:
        await session.execute(
            update(RecordVersion)
            .where(
                RecordVersion.screenshot_available.is_(True)
                | RecordVersion.screenshot_path.isnot(None)
                | RecordVersion.screenshot_expires_at.isnot(None)
                | RecordVersion.screenshot_width.isnot(None)
                | RecordVersion.screenshot_height.isnot(None)
            )
            .values(screenshot_available=False, screenshot_path=None,
                    screenshot_expires_at=None, screenshot_width=None,
                    screenshot_height=None)
        )

    if deleted:
        log.info("screenshot cleanup: removed %d files (errors %d)", deleted, errors)
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
