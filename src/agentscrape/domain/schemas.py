"""API request/response models. This is the contract the frontend codes against.

Naming follows the fixed contract in the brief: `area` is the normalized
specialty and `year` is the class-of year. The extra filters (role, pgy,
hospital, has_email) are additive and optional.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page[T](BaseModel):
    """Cursor pagination. `next_cursor` is null on the last page."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


class RecordOut(ApiModel):
    id: str
    site_id: str
    program_id: str | None = None
    hospital: str | None = None
    full_name: str | None
    email: str | None
    category: str = Field(
        description="resident | fellow | faculty | staff | student | alumni | unknown"
    )
    position: str | None = Field(
        default=None, description="Title exactly as the page printed it"
    )
    role_account: bool = False

    area: str | None = Field(default=None, description="Normalized specialty")
    area_raw: str | None = None
    year: int | None = Field(
        default=None, description="Class-of year, only when the page stated it"
    )

    pgy: int | None = Field(
        default=None,
        description="Training year exactly as printed; never rolled forward",
    )
    pgy_capture_date: date | None = Field(
        default=None, description="When the PGY above was read from the page"
    )

    status: str
    confidence: float
    version_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime | None = None
    missing_since: datetime | None = None
    screenshot_available: bool = False


class FieldChange(BaseModel):
    field: str
    previous: Any = None
    current: Any = None


class RecordVersionOut(ApiModel):
    id: str
    record_id: str
    version_no: int
    fields: dict[str, Any]
    changed_fields: list[FieldChange] = Field(
        default_factory=list,
        description="Backend-computed diff; the frontend never derives this.",
    )
    captured_at: datetime
    confidence: float
    source_url: str
    page_title: str | None = None
    extraction_method: str
    fetch_mode: str
    screenshot_available: bool
    screenshot_url: str | None = None
    screenshot_expires_at: datetime | None = None
    field_locations: dict[str, dict[str, int]] | None = None
    run_id: str | None = None


class SourceOut(BaseModel):
    """Provenance for one record version. Survives screenshot expiry."""

    record_id: str
    version_id: str
    version_no: int
    source_url: str
    page_title: str | None
    captured_at: datetime
    extraction_method: str
    fetch_mode: str
    confidence: float
    screenshot_available: bool
    screenshot_url: str | None
    screenshot_expires_at: datetime | None
    # Natural size of the screenshot. `field_locations` are in screenshot pixel
    # coordinates, so these are required to place them as percentages.
    screenshot_width: int | None = None
    screenshot_height: int | None = None
    field_locations: dict[str, dict[str, int]] | None
    run_id: str | None


class RecordStats(BaseModel):
    total: int
    by_status: dict[str, int]
    by_category: dict[str, int]
    by_area: dict[str, int]
    sites_covered: int
    recently_changed: int = Field(description="Changed in the last 30 days")
    with_email: int
    with_screenshot: int
    average_confidence: float
    average_versions: float


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


class RunConfigIn(BaseModel):
    concurrency: int = Field(default=1, ge=1, le=8)
    skip_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    step_budget: int = Field(default=40, ge=1, le=500)
    force_rescan: bool = False
    force_rescan_sites: list[str] = Field(
        default_factory=list, description="Site ids or domains to force individually"
    )
    max_records: int | None = Field(default=None, ge=1)
    max_spend_usd: float | None = Field(default=None, gt=0)
    label: str | None = None


class RunCreate(BaseModel):
    sites: list[str] = Field(min_length=1, description="Root URLs or domains")
    config: RunConfigIn = Field(default_factory=RunConfigIn)


class RunOut(ApiModel):
    id: str
    status: str
    label: str | None
    config: dict[str, Any]
    stop_reason: str | None
    error_message: str | None = None
    sites_total: int
    sites_completed: int
    sites_skipped: int
    sites_failed: int
    sites_rejected: int
    sites_pending: int = 0
    records_found: int
    records_new: int
    records_changed: int
    records_missing: int
    tokens_in: int
    tokens_out: int
    spend_usd: float
    max_records: int | None = None
    max_spend_usd: float | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SiteRunOut(ApiModel):
    id: str
    run_id: str
    site_id: str
    domain: str | None = None
    hospital: str | None = None
    status: str
    agent_id: str | None
    steps_taken: int
    step_budget: int
    skip_reason: str | None
    similarity_score: float | None
    records_found: int
    records_new: int
    records_changed: int
    records_missing: int
    known_path_hits: int
    candidates_considered: int
    error_code: str | None
    error_message: str | None
    spend_usd: float
    started_at: datetime | None
    finished_at: datetime | None


class CsvRowPreview(BaseModel):
    """One parsed school row retained for historical/admin request previews."""

    row: int
    input: str
    url: str | None
    valid: bool
    error: str | None = None
    site_id: str | None = None
    known_site: bool = False
    last_scraped_at: datetime | None = None
    known_path_count: int = 0
    previous_record_count: int = 0
    predicted_skip: bool = False
    validation_status: str | None = None
    validation_reason: str | None = None


class ValidatePreview(BaseModel):
    total_rows: int
    valid_rows: int
    invalid_rows: int
    known_sites: int
    predicted_skips: int
    rejected_sites: int
    rows: list[CsvRowPreview]


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------


class KnownPathOut(ApiModel):
    id: str
    url: str
    success_count: int
    failure_count: int
    consecutive_failures: int
    last_success_at: datetime | None
    avg_records: float
    score: float
    is_active: bool


class SchoolOut(ApiModel):
    """Matches the frontend's `School` type."""

    id: str
    name: str
    location: str | None = None
    root_domain: str
    canonical_url: str
    program_count: int = 0
    people_count: int = 0
    last_updated: datetime | None = None
    validation_status: str
    validation_reason: str | None = None


class ProgramOut(ApiModel):
    """Matches the frontend's `Program` type."""

    id: str
    school_id: str = Field(description="Owning school")
    name: str
    specialty: str
    type: str | None = Field(default=None, description="Residency / Fellowship")
    resident_count: int = 0
    fellow_count: int = 0
    people_count: int = 0
    last_updated: datetime | None = None
    start_url: str | None = None
    directory_url: str | None = None


class SiteOut(ApiModel):
    id: str
    root_domain: str
    canonical_url: str
    hospital: str | None = None
    validation_status: str
    validation_reason: str | None = None
    institution_type: str | None = None
    dominant_area: str | None = None
    last_scraped_at: datetime | None
    record_count: int = 0
    known_path_count: int = 0


class SiteDetail(SiteOut):
    known_paths: list[KnownPathOut] = Field(default_factory=list)
    recent_runs: list[SiteRunOut] = Field(default_factory=list)


class SchoolDetail(SchoolOut):
    known_paths: list[KnownPathOut] = Field(default_factory=list)
    recent_runs: list[SiteRunOut] = Field(default_factory=list)


class AdminSiteRow(SiteOut):
    total_site_runs: int = 0
    successful_site_runs: int = 0
    skipped_site_runs: int = 0
    failed_site_runs: int = 0
    success_rate: float = 0.0
    skip_rate: float = 0.0


class AdminStats(BaseModel):
    sites_total: int
    sites_rejected: int
    records_total: int
    records_active: int
    records_missing: int
    versions_total: int
    runs_total: int
    site_runs_total: int
    skip_rate: float = Field(description="Share of site runs that were skipped")
    known_path_hit_rate: float = Field(
        description="Share of completed site runs where a known path produced records"
    )
    known_paths_total: int
    known_paths_active: int
    total_spend_usd: float
    total_tokens_in: int
    total_tokens_out: int
    screenshots_on_disk: int
    screenshots_expired: int
    disk_bytes: int


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


class ExportCreate(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    include_provenance: bool = False
    include_emailed_column: bool = Field(
        default=False,
        description='Adds an empty "Has Been Emailed?" column for your own tracking.',
    )


class ExportOut(ApiModel):
    id: str
    status: str
    row_count: int | None
    download_url: str | None = None
    error: str | None
    created_at: datetime
    expires_at: datetime | None


class SubmissionOut(ApiModel):
    """Legacy staff-visible school request shape."""

    id: str
    filename: str | None
    note: str | None
    status: str
    row_count: int
    valid_count: int
    rows: list[CsvRowPreview] = Field(default_factory=list)
    run_id: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None


class SubmissionRunRequest(BaseModel):
    """Staff launching a legacy school request. One number: the budget."""

    max_spend_usd: float | None = Field(
        default=None, gt=0, description="Stop the run once estimated spend reaches this"
    )
    concurrency: int = Field(default=1, ge=1, le=8)
    force_rescan: bool = False


class MetaValues(BaseModel):
    values: list[str | int]


SortField = Literal["last_seen_at", "last_changed_at", "first_seen_at", "confidence", "full_name"]
