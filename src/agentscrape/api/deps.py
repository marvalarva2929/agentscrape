"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_sessionmaker
from .errors import AppError, ErrorCode
from .security import (
    ADMIN_SCOPE,
    CLIENT_SCOPE,
    decode_token,
    has_scope,
    token_from_request,
)


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
    """Any valid token. `?token=` is accepted too, because EventSource cannot
    set headers on the SSE stream. Returns the granted scope."""
    claims = decode_token(token_from_request(authorization, token))
    return str(claims.get("scope", CLIENT_SCOPE))


async def require_admin(
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> str:
    """Staff-only areas: submitted CSVs, launching runs, spend, site stats."""
    claims = decode_token(token_from_request(authorization, token))
    if not has_scope(claims, ADMIN_SCOPE):
        raise AppError(
            "This area requires the admin password.",
            code=ErrorCode.AUTH_FORBIDDEN,
            status_code=403,
        )
    return ADMIN_SCOPE


DbSession = Annotated[AsyncSession, Depends(get_db)]
AuthedUser = Annotated[str, Depends(require_auth)]
AdminUser = Annotated[str, Depends(require_admin)]
