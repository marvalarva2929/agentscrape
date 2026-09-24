"""Retention removes generated artifacts without losing a failed deletion."""
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentscrape.config import settings
from agentscrape.db.models import Export
from agentscrape.storage.artifacts import sweep_expired_exports


async def test_relative_export_path_and_old_orphans(session):
    settings.ensure_dirs()
    now = datetime.now(UTC)
    export = Export(status="completed", expires_at=now - timedelta(days=1), filters={})
    session.add(export)
    await session.flush()
    path = settings.export_dir / f"{export.id}.csv"
    path.write_text("expired")
    export.file_path = f"exports/{path.name}"
    old = settings.export_dir / f"exp_{'a' * 32}.csv"
    fresh = settings.export_dir / f"exp_{'b' * 32}.csv"
    unrelated = settings.export_dir / "important.csv"
    for file in [old, fresh, unrelated]:
        file.write_text("keep unless old generated orphan")
    timestamp = (now - timedelta(days=30)).timestamp()
    os.utime(old, (timestamp, timestamp))
    os.utime(unrelated, (timestamp, timestamp))
    await session.commit()
    result = await sweep_expired_exports(now=now)
    await session.refresh(export)
    assert result == {"expired": 1, "orphaned": 1}
    assert export.status == "expired" and export.file_path is None
    assert not path.exists() and not old.exists()
    assert fresh.exists() and unrelated.exists()


async def test_failed_unlink_keeps_job_for_retry(session, monkeypatch):
    settings.ensure_dirs()
    now = datetime.now(UTC)
    export = Export(status="completed", expires_at=now - timedelta(days=1),
                    file_path="exports/retry.csv", filters={})
    session.add(export)
    await session.commit()
    def denied(*args, **kwargs):
        raise PermissionError("busy")
    monkeypatch.setattr(Path, "unlink", denied)
    assert (await sweep_expired_exports(now=now))["expired"] == 0
    await session.refresh(export)
    assert export.status == "completed"
    assert export.file_path == "exports/retry.csv"
