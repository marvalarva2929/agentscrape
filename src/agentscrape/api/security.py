"""Two passwords, two scopes, bearer tokens.

A client password grants `client` scope: browse schools, people, sources and
exports. A separate admin password grants `admin` scope: launch billable runs
and view spend. Both are plain config values — no user table, no roles.

Bearer tokens rather than cookies, deliberately. The frontend is served from
GitHub Pages, so a session cookie would be third-party to the API's origin,
requiring SameSite=None and being blocked outright by Safari and increasingly by
Chrome. That would work locally and fail in production, which is the worst
failure mode available.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import jwt

from ..config import settings
from .errors import AuthRequiredError, ErrorCode, InvalidTokenError

_HKDF_INFO = b"agentscrape-auth-v1"
ISSUER = "agentscrape"

CLIENT_SCOPE = "client"
ADMIN_SCOPE = "admin"
# Admin can do anything the client can.
_SCOPE_RANK = {CLIENT_SCOPE: 1, ADMIN_SCOPE: 2}


def _hkdf_secret(password: str) -> bytes:
    """HKDF-Extract/Expand over the configured password (RFC 5869, one block)."""
    prk = hmac.new(b"agentscrape-salt-v1", password.encode(), hashlib.sha256).digest()
    return hmac.new(prk, _HKDF_INFO + b"\x01", hashlib.sha256).digest()


def _secret() -> bytes:
    """Signing key. Derived from both passwords so rotating either invalidates
    every outstanding token."""
    return _hkdf_secret(f"{settings.app_password}\x00{settings.admin_password}")


def scope_for_password(candidate: str) -> str | None:
    """Which scope this password grants, or None if it matches neither.

    Admin is checked first so that configuring both to the same value grants the
    higher scope rather than silently locking the admin out.
    """
    if hmac.compare_digest(candidate.encode(), settings.admin_password.encode()):
        return ADMIN_SCOPE
    if hmac.compare_digest(candidate.encode(), settings.app_password.encode()):
        return CLIENT_SCOPE
    return None


def verify_password(candidate: str) -> bool:
    return scope_for_password(candidate) is not None


def issue_token(scope: str = CLIENT_SCOPE) -> tuple[str, datetime]:
    expires_at = datetime.now(UTC) + timedelta(hours=settings.token_ttl_hours)
    payload = {
        "iss": ISSUER,
        "sub": scope,
        "scope": scope,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256"), expires_at


def has_scope(claims: dict, required: str) -> bool:
    granted = str(claims.get("scope", CLIENT_SCOPE))
    return _SCOPE_RANK.get(granted, 0) >= _SCOPE_RANK.get(required, 99)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(
            token, _secret(), algorithms=["HS256"], issuer=ISSUER,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError(
            "Session expired. Sign in again.", code=ErrorCode.AUTH_INVALID_TOKEN
        ) from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid credential.") from exc


def token_from_request(authorization: str | None, query_token: str | None) -> str:
    """Bearer header normally; `?token=` only for the SSE stream, which cannot
    set headers from EventSource."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    if query_token:
        return query_token
    raise AuthRequiredError("Authentication required.")
