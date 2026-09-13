"""Enumerations shared by the ORM, the API schemas and the pipeline."""

from __future__ import annotations

from enum import StrEnum


class ValidationStatus(StrEnum):
    PENDING = "pending"
    POST_SECONDARY = "post_secondary"
    K12_REJECTED = "k12_rejected"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STOPPED_AT_LIMIT = "stopped_at_limit"
    FAILED = "failed"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.STOPPED_AT_LIMIT, RunStatus.FAILED}
)


class SiteRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL_SITE_RUN_STATUSES = frozenset(
    {
        SiteRunStatus.COMPLETED,
        SiteRunStatus.SKIPPED,
        SiteRunStatus.FAILED,
        SiteRunStatus.REJECTED,
        SiteRunStatus.CANCELLED,
    }
)


class RecordStatus(StrEnum):
    ACTIVE = "active"
    NEW = "new"
    CHANGED = "changed"
    MISSING = "missing"


class RecordRole(StrEnum):
    RESIDENT = "resident"
    FELLOW = "fellow"
    UNKNOWN = "unknown"


class IdentityKind(StrEnum):
    EMAIL = "email"
    NAME = "name"


class ExtractionMethod(StrEnum):
    KNOWN_PATH = "known_path"
    DISCOVERY = "discovery"
    AGENT_NAV = "agent_nav"


class FetchMode(StrEnum):
    HTML = "html"
    RENDER = "render"
    BOTH = "both"


class SkipReason(StrEnum):
    UNCHANGED_FINGERPRINT = "unchanged_fingerprint"
    SIMILARITY_THRESHOLD = "similarity_threshold"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class FieldSource(StrEnum):
    EXTRACTED = "extracted"
    DERIVED = "derived"


class StopReason(StrEnum):
    MAX_RECORDS = "max_records"
    MAX_SPEND = "max_spend"
    CANCELLED = "cancelled"
    RUN_TIMEOUT = "run_timeout"
