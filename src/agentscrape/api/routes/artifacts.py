"""Serve screenshots from local disk.

Authenticated like everything else, and path-guarded: a version's stored path is
relative to ARTIFACT_DIR and must resolve back inside it.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ...config import settings
from ..deps import AuthedUser
from ..errors import AppError, ErrorCode, NotFoundError

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{path:path}")
async def get_artifact(path: str, _: AuthedUser) -> FileResponse:
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
