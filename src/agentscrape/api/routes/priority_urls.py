"""Extract priority URLs from a client-supplied spreadsheet or Google Sheet.

Preview-only: nothing here touches a run or the database. The client reviews
(and can prune) the extracted list in the frontend, then sends whatever's left
as `config.priority_urls` on the normal `POST /runs` call.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, File, UploadFile

from ...discovery.priority_urls import (
    extract_urls_from_csv,
    extract_urls_from_xlsx,
    google_sheet_csv_export_url,
)
from ...domain.schemas import GoogleSheetPriorityUrlsRequest, PriorityUrlsPreview
from ..deps import AuthedUser
from ..errors import AppError, ErrorCode

router = APIRouter(tags=["priority-urls"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/priority-urls/upload", response_model=PriorityUrlsPreview)
async def upload_priority_urls(_: AuthedUser, file: UploadFile = File(...)) -> PriorityUrlsPreview:
    """Scan every cell of an uploaded .xlsx or every field of a .csv for
    recognizable http(s) URLs. Column-agnostic and worksheet-agnostic by
    design - a URL may sit anywhere in the workbook."""
    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise AppError("File is too large (10MB limit).", code=ErrorCode.VALIDATION_ERROR)
    name = (file.filename or "").lower()
    try:
        if name.endswith(".xlsx"):
            urls = extract_urls_from_xlsx(content)
        elif name.endswith(".csv"):
            urls = extract_urls_from_csv(content)
        else:
            raise AppError(
                "Only .xlsx or .csv files are supported.", code=ErrorCode.VALIDATION_ERROR
            )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            f"Could not read {file.filename!r}: {exc}", code=ErrorCode.VALIDATION_ERROR
        ) from exc
    return PriorityUrlsPreview(urls=urls)


@router.post("/priority-urls/google-sheet", response_model=PriorityUrlsPreview)
async def google_sheet_priority_urls(
    body: GoogleSheetPriorityUrlsRequest, _: AuthedUser
) -> PriorityUrlsPreview:
    """A publicly/link-shared Google Sheet, fetched as a CSV export - no
    OAuth. v1 scope: one worksheet (whichever `gid` the link encodes)."""
    export_url = google_sheet_csv_export_url(body.sheet_url)
    if export_url is None:
        raise AppError("That doesn't look like a Google Sheets URL.", code=ErrorCode.VALIDATION_ERROR)
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(export_url)
    except httpx.HTTPError as exc:
        raise AppError(f"Could not reach that Google Sheet: {exc}", code=ErrorCode.VALIDATION_ERROR) from exc
    if response.status_code != 200 or "text/csv" not in response.headers.get("content-type", ""):
        raise AppError(
            "That Google Sheet isn't publicly viewable (share it as "
            "\"Anyone with the link\" and try again).",
            code=ErrorCode.VALIDATION_ERROR,
        )
    return PriorityUrlsPreview(urls=extract_urls_from_csv(response.content))
