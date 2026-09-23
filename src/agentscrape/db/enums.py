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


# What a queued run does when its turn comes: crawl its schools, or re-check
# already-scraped role labels (a VerificationJob with this run's id).
RUN_KIND_CRAWL = "crawl"
RUN_KIND_VERIFY = "verify"


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


class PersonCategory(StrEnum):
    """Coarse bucket for filtering. The person's printed title is kept verbatim
    alongside this in `Record.position`.

    Everyone published on an institution's site is collected and labelled; this
    is a classification, not a filter. It used to be resident/fellow only, with
    everyone else discarded.
    """

    RESIDENT = "resident"
    FELLOW = "fellow"
    FACULTY = "faculty"
    STAFF = "staff"
    STUDENT = "student"
    ALUMNI = "alumni"
    UNKNOWN = "unknown"


# The trainee categories, used for the resident/fellow counts the UI shows.
TRAINEE_CATEGORIES = frozenset({PersonCategory.RESIDENT, PersonCategory.FELLOW})


class IdentityKind(StrEnum):
    EMAIL = "email"
    NAME = "name"


class ExtractionMethod(StrEnum):
    KNOWN_PATH = "known_path"
    DISCOVERY = "discovery"
    AGENT_NAV = "agent_nav"
    # Filled in by searching the institution's people directory.
    DIRECTORY = "directory"
    # A verification job's finding promoted into `category`, not a fresh page
    # read. `extraction_method` is a plain VARCHAR(10), so this stays short.
    VERIFY = "verify"


class FetchMode(StrEnum):
    HTML = "html"
    RENDER = "render"
    BOTH = "both"


class SkipReason(StrEnum):
    UNCHANGED_FINGERPRINT = "unchanged_fingerprint"
    SIMILARITY_THRESHOLD = "similarity_threshold"


class SubmissionStatus(StrEnum):
    """Lifecycle of a legacy school request."""

    PENDING = "pending"       # submitted, awaiting staff review
    RUNNING = "running"       # a run was launched from it
    DONE = "done"
    REJECTED = "rejected"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StopReason(StrEnum):
    MAX_RECORDS = "max_records"
    # Residents and fellows collected, and people with an address.
    MAX_TRAINEES = "max_trainees"
    MAX_EMAILS = "max_emails"
    MAX_SPEND = "max_spend"
    CANCELLED = "cancelled"
    RUN_TIMEOUT = "run_timeout"
