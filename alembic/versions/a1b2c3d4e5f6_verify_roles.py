"""add record.roles + roles_checked_at, and verification_jobs

Revision ID: a1b2c3d4e5f6
Revises: f6a7b8c9d0e1
Create Date: 2026-09-21 20:00:00.000000

On-demand role verification: a person's crawl-picked `category` is a single
bucket, but the source page can support more than one (faculty *and* fellow).
`roles` holds the full set once a verification job has read the page;
`verification_jobs` tracks those jobs, which run only when asked, never as
part of a crawl.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("records", sa.Column("roles", _JSON, nullable=True))
    op.add_column(
        "records",
        sa.Column("roles_checked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "verification_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "running", "completed", "failed",
                name="verification_status", native_enum=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "site_id", sa.String(64),
            sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("record_ids", _JSON, nullable=True),
        sa.Column("records_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_corrected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_verification_jobs_status", "verification_jobs", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_verification_jobs_status", table_name="verification_jobs")
    op.drop_table("verification_jobs")
    op.drop_column("records", "roles_checked_at")
    op.drop_column("records", "roles")
