"""Opaque keyset cursors. Never OFFSET — pages must stay stable while rows are
being written by a live run."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from .errors import AppError, ErrorCode

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Cursor:
    sort_value: Any
    id: str

    def encode(self) -> str:
        raw = json.dumps({"v": self.sort_value, "i": self.id}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @staticmethod
    def decode(token: str | None) -> Cursor | None:
        if not token:
            return None
        try:
            padded = token + "=" * (-len(token) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            return Cursor(sort_value=data["v"], id=data["i"])
        except Exception as exc:
            raise AppError(
                "Malformed pagination cursor.", code=ErrorCode.INVALID_CURSOR
            ) from exc


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))
