"""Exported CSVs on local disk with a configurable retention window."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from ..config import settings
from ..db.models import Export
from ..db.session import session_scope

log = logging.getLogger("agentscrape.artifacts")


def relative_path(path: Path) -> str:
    """Store paths relative to ARTIFACT_DIR so the directory can be relocated."""
    try:
        return str(path.resolve().relative_to(settings.artifact_dir.resolve()))
    except ValueError:
        return str(path)


def absolute_path(stored: str) -> Path:
    candidate = Path(stored)
    return candidate if candidate.is_absolute() else settings.artifact_dir / stored


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
                    absolute_path(file_path).unlink(missing_ok=True)
                except OSError:
                    log.exception("export sweep: could not remove %s; will retry", export_id)
                    continue
            await session.execute(
                update(Export)
                .where(Export.id == export_id)
                .values(status=ExportStatus.EXPIRED, file_path=None)
            )
            removed += 1
    if removed:
        log.info("export sweep: expired %d", removed)
    # Failed/interrupted jobs and old test runs can leave unreferenced CSVs.
    # Only generated filenames older than the retention window qualify; keep
    # every file belonging to a live or completed job, even while it is writing.
    orphaned = 0
    if settings.export_retention_days is not None:
        cutoff = (now - timedelta(days=max(1, settings.export_retention_days))).timestamp()
        async with session_scope() as session:
            protected = set((await session.scalars(select(Export.id).where(
                Export.status.in_([ExportStatus.PENDING, ExportStatus.RUNNING, ExportStatus.COMPLETED])
            ))).all())
            for path in settings.export_dir.glob("exp_*.csv"):
                suffix = path.stem.removeprefix("exp_")
                if len(suffix) != 32 or any(c not in "0123456789abcdef" for c in suffix):
                    continue
                if path.stem in protected or path.is_symlink():
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        orphaned += 1
                except OSError:
                    log.exception("export sweep: could not remove orphan %s", path.name)
    return {"expired": removed, "orphaned": orphaned}
