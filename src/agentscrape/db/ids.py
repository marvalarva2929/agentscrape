"""Opaque string IDs, per the API contract.

Prefixed so a stray ID in a log or a bug report is self-describing, but callers
must treat them as opaque and never parse them.
"""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def site_id() -> str:
    return new_id("site")


def run_id() -> str:
    return new_id("run")


def site_run_id() -> str:
    return new_id("sr")


def program_id() -> str:
    return new_id("prog")


def record_id() -> str:
    return new_id("rec")


def version_id() -> str:
    return new_id("ver")


def path_id() -> str:
    return new_id("path")


def submission_id() -> str:
    return new_id("sub")


def export_id() -> str:
    return new_id("exp")
