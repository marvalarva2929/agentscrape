"""Single-credential auth. No users, no roles, no registration."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..deps import AuthedUser
from ..errors import AppError, ErrorCode
from ..security import ADMIN_SCOPE, issue_token, scope_for_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class SessionUser(BaseModel):
    name: str
    scope: str


class LoginResponse(BaseModel):
    """Shaped for the frontend's `AuthSession`, plus the token it must store."""

    authenticated: bool = True
    user: SessionUser
    token: str
    expires_at: str
    token_type: str = "bearer"


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    scope = scope_for_password(body.password)
    if scope is None:
        raise AppError(
            "Incorrect password.",
            code=ErrorCode.AUTH_INVALID_PASSWORD,
            status_code=401,
        )
    token, expires_at = issue_token(scope)
    return LoginResponse(
        user=SessionUser(name="Staff" if scope == ADMIN_SCOPE else "Operator", scope=scope),
        token=token,
        expires_at=expires_at.isoformat(),
    )


@router.get("/session", response_model=LoginResponse | dict)
async def session(scope: AuthedUser) -> dict:
    """Lets the frontend restore a session on reload without re-prompting."""
    return {
        "authenticated": True,
        "user": {"name": "Staff" if scope == ADMIN_SCOPE else "Operator", "scope": scope},
    }


@router.post("/logout", status_code=204)
async def logout() -> None:
    """Tokens are stateless, so the client simply discards it. Present because
    the frontend calls it, and so logout stays a single code path there."""
    return None
