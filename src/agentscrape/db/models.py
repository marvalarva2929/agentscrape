"""SQLAlchemy models. Provenance fields are present from the first migration."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from . import enums as e
from .ids import (
    export_id,
    path_id,
    program_id,
    record_id,
    run_id,
    site_id,
    site_run_id,
    submission_id,
    version_id,
)

# none_as_null matters: without it SQLAlchemy stores a Python None in a JSON
# column as the JSON value `null` rather than SQL NULL, so `IS NULL` predicates
# silently never match and a cleared checkpoint still looks present.
JSONType = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)


def _enum(python_enum: type, name: str) -> Enum:
    """VARCHAR + CHECK rather than a native PG type: adding a value later is one
    constraint swap instead of a type migration."""
    return Enum(python_enum, name=name, native_enum=False, values_callable=lambda c: [m.value for m in c])


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Site(TimestampMixin, Base):
    """One institution's web presence. Persists across runs."""

    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=site_id)
    root_domain: Mapped[str] = mapped_column(String(253), nullable=False, unique=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    hospital_name: Mapped[str | None] = mapped_column(Text)
    # Display fields for the school list. `name` falls back to the domain.
    name: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    validation_status: Mapped[str] = mapped_column(
        _enum(e.ValidationStatus, "validation_status"),
        default=e.ValidationStatus.PENDING, nullable=False,
    )
    validation_reason: Mapped[str | None] = mapped_column(Text)
    institution_type: Mapped[str | None] = mapped_column(String(64))

    # Fallback when a page's own specialty cannot be inferred.
    dominant_specialty: Mapped[str | None] = mapped_column(String(128))

    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Content hashes of the known-good paths at last scrape; tier-1 skip check.
    last_fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    # The institution's people/student directory, from the school sheet, and
    # how to search it once learned (see `directory.learn`), so later runs skip
    # the learning step.
    directory_url: Mapped[str | None] = mapped_column(Text)
    directory_config: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    # Other registrable domains the institution publishes on (its website's,
    # when the sheet's residency hub lives on another one). In crawl scope.
    affiliated_domains: Mapped[list[str] | None] = mapped_column(JSONType)
    # The client's fixed school list: only active schools are offered for a
    # crawl. Inactive rows keep their people and history.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    records: Mapped[list[Record]] = relationship(back_populates="site")
    known_paths: Mapped[list[KnownPath]] = relationship(back_populates="site")

    __table_args__ = (Index("ix_sites_last_scraped_at", "last_scraped_at"),)


class Run(TimestampMixin, Base):
    """One execution over a list of sites."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=run_id)
    status: Mapped[str] = mapped_column(
        _enum(e.RunStatus, "run_status"), default=e.RunStatus.PENDING, nullable=False
    )
    label: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(_enum(e.StopReason, "stop_reason"))
    error_message: Mapped[str | None] = mapped_column(Text)

    sites_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sites_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sites_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sites_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sites_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    records_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_changed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_missing: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Live spend meter, updated as each model call returns — never computed at the end.
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    spend_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Waits its turn for the one model budget instead of starting beside the
    # run that holds it. Every run made through the API is queued.
    queued: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # Order among waiting runs, 1 first. Ties fall back to created_at, then id.
    queue_rank: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # Written every couple of seconds by the orchestrator that owns the run, so
    # a run whose process died can be told apart from one that is still working.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site_runs: Mapped[list[SiteRun]] = relationship(back_populates="run")

    __table_args__ = (
        Index("ix_runs_status_created", "status", "created_at"),
        # The database refuses a second running queued run, whatever process or
        # code path tries to start it. This is what makes "one at a time" a
        # guarantee instead of a convention.
        Index(
            "ux_runs_one_running_queued", "queued", unique=True,
            postgresql_where=text("status = 'running' AND queued"),
        ),
    )


class SiteRun(TimestampMixin, Base):
    """One site's participation in one run. Also the orchestrator's work queue row."""

    __tablename__ = "site_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=site_run_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        _enum(e.SiteRunStatus, "site_run_status"),
        default=e.SiteRunStatus.PENDING, nullable=False,
    )
    agent_id: Mapped[str | None] = mapped_column(String(64))
    force_rescan: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Crawl order inside the run: created_at is one shared timestamp for every
    # school made together, so it cannot say which goes first.
    position: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    steps_taken: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    step_budget: Mapped[int] = mapped_column(Integer, default=40, nullable=False)

    skip_reason: Mapped[str | None] = mapped_column(_enum(e.SkipReason, "skip_reason"))
    similarity_score: Mapped[float | None] = mapped_column(Float)

    records_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_changed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_missing: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    known_path_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidates_considered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    spend_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)

    # LangGraph checkpoint pointer + resumable pipeline state.
    checkpoint_state: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run] = relationship(back_populates="site_runs")
    site: Mapped[Site] = relationship()

    __table_args__ = (
        UniqueConstraint("run_id", "site_id", name="uq_site_runs_run_site"),
        # The queue claim scans this.
        Index("ix_site_runs_run_status", "run_id", "status"),
        Index("ix_site_runs_site", "site_id"),
    )


class SiteRunVisit(Base):
    """Every URL touched during one SiteRun.

    This is the in-site dedupe set (multiple entry points must not re-fetch the
    same page) and the resume anchor (a restarted run skips what it already did).
    """

    __tablename__ = "site_run_visits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_run_id: Mapped[str] = mapped_column(
        ForeignKey("site_runs.id", ondelete="CASCADE"), nullable=False
    )
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    fetch_mode: Mapped[str | None] = mapped_column(_enum(e.FetchMode, "fetch_mode"))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    records_yielded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("site_run_id", "url_hash", name="uq_visit_site_run_url"),
    )


class Program(TimestampMixin, Base):
    """One training programme at a school, e.g. Internal Medicine Residency.

    Keyed on (site, normalized specialty). Rows are created automatically as
    people are extracted, and can also be created directly so a newly added
    school can be configured before it has any data.
    """

    __tablename__ = "programs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=program_id)
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    specialty: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    program_type: Mapped[str | None] = mapped_column(String(64))

    # Entry points for a targeted re-crawl of just this programme.
    start_url: Mapped[str | None] = mapped_column(Text)
    directory_url: Mapped[str | None] = mapped_column(Text)

    people_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resident_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fellow_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship()

    __table_args__ = (
        UniqueConstraint("site_id", "specialty", name="uq_programs_site_specialty"),
        Index("ix_programs_site", "site_id"),
    )


class Record(TimestampMixin, Base):
    """A person found at a site. Stable identity across runs. Never deleted."""

    __tablename__ = "records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=record_id)
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    program_id: Mapped[str | None] = mapped_column(
        ForeignKey("programs.id", ondelete="SET NULL")
    )
    identity_key: Mapped[str] = mapped_column(String(320), nullable=False)
    identity_kind: Mapped[str] = mapped_column(
        _enum(e.IdentityKind, "identity_kind"), nullable=False
    )
    role_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    full_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    # Coarse bucket for filtering: resident / fellow / faculty / staff /
    # student / alumni / unknown. Everyone published on the site is stored.
    category: Mapped[str] = mapped_column(
        _enum(e.PersonCategory, "person_category"),
        default=e.PersonCategory.UNKNOWN,
        nullable=False,
    )
    # The person's title exactly as the page printed it.
    position: Mapped[str | None] = mapped_column(Text)

    # `area` in the API contract.
    specialty_normalized: Mapped[str | None] = mapped_column(String(128))
    specialty_raw: Mapped[str | None] = mapped_column(Text)

    # Stored exactly as the page printed it. Nothing is inferred or rolled
    # forward, so the capture date beside it is what gives it meaning.
    pgy_at_capture: Mapped[int | None] = mapped_column(Integer)
    pgy_capture_date: Mapped[date | None] = mapped_column(Date)
    class_of: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(
        _enum(e.RecordStatus, "record_status"), default=e.RecordStatus.NEW, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    current_version_id: Mapped[str | None] = mapped_column(String(64))
    version_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[str | None] = mapped_column(String(64))

    site: Mapped[Site] = relationship(back_populates="records")
    versions: Mapped[list[RecordVersion]] = relationship(
        back_populates="record", order_by="RecordVersion.version_no"
    )

    __table_args__ = (
        UniqueConstraint("site_id", "identity_key", name="uq_records_site_identity"),
        Index("ix_records_site_status", "site_id", "status"),
        Index("ix_records_program", "program_id"),
        Index("ix_records_specialty", "specialty_normalized"),
        Index("ix_records_class_of", "class_of"),
        Index("ix_records_category", "category"),
        Index("ix_records_last_changed", "last_changed_at"),
        Index("ix_records_last_seen", "last_seen_at"),
        Index("ix_records_email", "email"),
    )


class RecordVersion(Base):
    """Immutable snapshot, written only when something changed. Carries provenance.

    Provenance outlives the screenshot: url, title, timestamp, method and field
    locations are kept after the image is swept, and `screenshot_available` tells
    the frontend which of the two states it is rendering.
    """

    __tablename__ = "record_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=version_id)
    record_id: Mapped[str] = mapped_column(
        ForeignKey("records.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)

    fields: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    changed_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )

    # --- provenance ---
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_title: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    screenshot_available: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    screenshot_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Natural pixel size of the screenshot. The frontend needs it to place the
    # field boxes below, which are stored in screenshot pixel coordinates.
    screenshot_width: Mapped[int | None] = mapped_column(Integer)
    screenshot_height: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    extraction_method: Mapped[str] = mapped_column(
        _enum(e.ExtractionMethod, "extraction_method"), nullable=False
    )
    fetch_mode: Mapped[str] = mapped_column(_enum(e.FetchMode, "fetch_mode_v"), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # {field: {"x":..,"y":..,"width":..,"height":..}} in screenshot pixel space.
    field_locations: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    run_id: Mapped[str | None] = mapped_column(String(64))
    site_run_id: Mapped[str | None] = mapped_column(String(64))

    record: Mapped[Record] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("record_id", "version_no", name="uq_version_record_no"),
        Index("ix_versions_record", "record_id", "version_no"),
        Index("ix_versions_captured", "captured_at"),
        Index("ix_versions_screenshot_expiry", "screenshot_expires_at"),
        CheckConstraint("version_no > 0", name="ck_version_no_positive"),
    )


class KnownPath(TimestampMixin, Base):
    """A URL that previously yielded records for a site, with success statistics.

    Known paths jump to the front of the candidate list. A path that stops
    producing decays and is eventually deactivated.
    """

    __tablename__ = "known_paths"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=path_id)
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    avg_records: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_content_hash: Mapped[str | None] = mapped_column(String(64))

    site: Mapped[Site] = relationship(back_populates="known_paths")

    __table_args__ = (
        UniqueConstraint("site_id", "url_hash", name="uq_known_paths_site_url"),
        Index("ix_known_paths_site_active", "site_id", "is_active", "score"),
    )


class CsvSubmission(TimestampMixin, Base):
    """A client's request for schools to be crawled.

    Clients cannot start runs — billing is per school, so staff review a
    submission and launch it from the admin area.
    """

    __tablename__ = "csv_submissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=submission_id)
    filename: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        _enum(e.SubmissionStatus, "submission_status"),
        default=e.SubmissionStatus.PENDING,
        nullable=False,
    )
    # Parsed rows: [{"row": 1, "input": "...", "url": "...", "valid": true, ...}]
    rows: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    valid_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    submitted_by: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_submissions_status", "status", "created_at"),)


class Export(TimestampMixin, Base):
    """Async filtered CSV export job."""

    __tablename__ = "exports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=export_id)
    status: Mapped[str] = mapped_column(
        _enum(e.ExportStatus, "export_status"), default=e.ExportStatus.PENDING, nullable=False
    )
    filters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    include_provenance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Emits an empty "Has Been Emailed?" column for the client's own tracking.
    include_emailed_column: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    file_path: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_exports_status", "status", "created_at"),)
