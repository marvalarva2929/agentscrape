"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_sessionmaker
from .security import decode_token, token_from_request


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def require_auth(
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> str:
    """Bearer token on every request. `?token=` is accepted too, because
    EventSource cannot set headers on the SSE stream."""
    decode_token(token_from_request(authorization, token))
    return "operator"


DbSession = Annotated[AsyncSession, Depends(get_db)]
AuthedUser = Annotated[str, Depends(require_auth)]
