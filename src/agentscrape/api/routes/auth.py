"""Single-credential auth. No users, no roles, no registration."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..errors import AppError, ErrorCode
from ..security import issue_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    token: str
    expires_at: str
    token_type: str = "bearer"


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    if not verify_password(body.password):
        raise AppError(
            "Incorrect password.",
            code=ErrorCode.AUTH_INVALID_PASSWORD,
            status_code=401,
        )
    token, expires_at = issue_token()
    return LoginResponse(token=token, expires_at=expires_at.isoformat())
