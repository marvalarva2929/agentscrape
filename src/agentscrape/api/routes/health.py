"""Liveness. Unauthenticated on purpose: the frontend polls it to tell
'server stopped' apart from 'not logged in'."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import text

from ...db.session import get_sessionmaker

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    database_ok = True
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False
    return {
        "status": "ok" if database_ok else "degraded",
        "database": "up" if database_ok else "down",
        "time": datetime.now(UTC).isoformat(),
    }
