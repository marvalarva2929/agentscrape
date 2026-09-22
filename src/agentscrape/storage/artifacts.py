"""Exported CSVs on local disk with a configurable retention window."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
