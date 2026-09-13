"""Error envelope. Every non-2xx returns {"error": {code, message, details}}.

The frontend branches on `code` and displays `message`, so codes are a contract:
add new ones freely, never repurpose an existing one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    # auth
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
    AUTH_INVALID_PASSWORD = "AUTH_INVALID_PASSWORD"
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
    # request
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INVALID_CURSOR = "INVALID_CURSOR"
    INVALID_CSV = "INVALID_CSV"
    # domain
    K12_INSTITUTION_REJECTED = "K12_INSTITUTION_REJECTED"
    SITE_UNREACHABLE = "SITE_UNREACHABLE"
    RUN_NOT_CANCELLABLE = "RUN_NOT_CANCELLABLE"
    SITE_NOT_RETRYABLE = "SITE_NOT_RETRYABLE"
    EXPORT_NOT_READY = "EXPORT_NOT_READY"
    SCREENSHOT_EXPIRED = "SCREENSHOT_EXPIRED"
    # capacity
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    STEP_BUDGET_EXCEEDED = "STEP_BUDGET_EXCEEDED"
    RUN_LIMIT_REACHED = "RUN_LIMIT_REACHED"
    # infra
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """Base for every error that should reach the client as a clean envelope."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: ErrorCode = ErrorCode.VALIDATION_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details

    def to_response(self) -> JSONResponse:
        payload: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.details:
            payload["details"] = jsonable_encoder(self.details)
        return JSONResponse(status_code=self.status_code, content={"error": payload})


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.CONFLICT


class AuthRequiredError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.AUTH_REQUIRED


class InvalidTokenError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.AUTH_INVALID_TOKEN


class ResourceLimitError(AppError):
    status_code = status.HTTP_507_INSUFFICIENT_STORAGE
    code = ErrorCode.RESOURCE_LIMIT_EXCEEDED


def register_exception_handlers(app) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": str(ErrorCode.VALIDATION_ERROR),
                    "message": "Request validation failed.",
                    "details": {"errors": jsonable_encoder(exc.errors())},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = (
            ErrorCode.NOT_FOUND
            if exc.status_code == 404
            else ErrorCode.AUTH_REQUIRED
            if exc.status_code == 401
            else ErrorCode.VALIDATION_ERROR
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": str(code), "message": str(exc.detail)}},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("agentscrape").exception("unhandled error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": str(ErrorCode.INTERNAL_ERROR),
                    "message": "An unexpected error occurred.",
                }
            },
        )
