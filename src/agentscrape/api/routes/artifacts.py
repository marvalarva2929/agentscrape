"""Serve screenshots from local disk.

Authenticated like everything else, and path-guarded: a version's stored path is
relative to ARTIFACT_DIR and must resolve back inside it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import FileResponse

from ...config import settings
from ..errors import AppError, ErrorCode, NotFoundError
from ..security import (
    decode_token,
    token_from_request,
    verify_artifact_signature,
)

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{path:path}")
async def get_artifact(
    path: str,
    exp: str | None = None,
    sig: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query(include_in_schema=False)] = None,
) -> FileResponse:
    # An <img> tag cannot send an Authorization header, so a short-lived signed
    # link is accepted as well as a normal token.
    if not verify_artifact_signature(path, exp, sig):
        decode_token(token_from_request(authorization, token))

    root = settings.artifact_dir.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise AppError(
            "Invalid artifact path.", code=ErrorCode.VALIDATION_ERROR, status_code=400
        )
    if not target.is_file():
        # Distinguished from a 404 so the frontend can render "screenshot expired"
        # rather than "something went wrong".
        raise NotFoundError(
            "This screenshot is no longer available; it passed its retention window.",
        )
    return FileResponse(target)
