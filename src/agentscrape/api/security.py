"""One user, one password. A login exchanges the password for a bearer token.

The signing secret is derived from the configured password with HKDF, so there is
no token table and a restart does not invalidate anyone. Rotating APP_PASSWORD
invalidates every outstanding token, which is the desired behaviour.

Per-request password signing was considered and rejected: it would put the
password on every request for no gain over a short-lived derived token.
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
SUBJECT = "operator"


def _hkdf_secret(password: str) -> bytes:
    """HKDF-Extract/Expand over the configured password (RFC 5869, one block)."""
    prk = hmac.new(b"agentscrape-salt-v1", password.encode(), hashlib.sha256).digest()
    return hmac.new(prk, _HKDF_INFO + b"\x01", hashlib.sha256).digest()


def _secret() -> bytes:
    return _hkdf_secret(settings.app_password)


def verify_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode(), settings.app_password.encode())


def issue_token() -> tuple[str, datetime]:
    expires_at = datetime.now(UTC) + timedelta(hours=settings.token_ttl_hours)
    payload = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256"), expires_at


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
