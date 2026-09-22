"""drop screenshot storage: columns, index, and the on-disk artifacts they pointed to

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-21 19:00:00.000000

Screenshots were never swept for failed/cancelled runs and defaulted to
SCREENSHOT_RETENTION_DAYS="" (keep forever), so artifacts/screenshots/ grew
without bound. The feature is removed: crawls keep only the extracted data.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_versions_screenshot_expiry", table_name="record_versions")
    op.drop_column("record_versions", "screenshot_path")
    op.drop_column("record_versions", "screenshot_available")
    op.drop_column("record_versions", "screenshot_expires_at")
    op.drop_column("record_versions", "screenshot_width")
    op.drop_column("record_versions", "screenshot_height")
    op.drop_column("record_versions", "field_locations")


def downgrade() -> None:
    op.add_column("record_versions", sa.Column("field_locations", sa.JSON(), nullable=True))
    op.add_column("record_versions", sa.Column("screenshot_height", sa.Integer(), nullable=True))
    op.add_column("record_versions", sa.Column("screenshot_width", sa.Integer(), nullable=True))
    op.add_column(
        "record_versions",
        sa.Column("screenshot_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "record_versions",
        sa.Column(
            "screenshot_available", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("record_versions", sa.Column("screenshot_path", sa.Text(), nullable=True))
    op.create_index(
        "ix_versions_screenshot_expiry", "record_versions", ["screenshot_expires_at"]
    )
