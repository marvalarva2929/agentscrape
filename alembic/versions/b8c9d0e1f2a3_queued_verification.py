"""run.kind + verification_jobs.run_id: verification waits in the run queue

Revision ID: b8c9d0e1f2a3
Revises: a1b2c3d4e5f6
Create Date: 2026-09-23 12:00:00.000000

A verification pass reads every source page again and calls the model for
each, the same budget a crawl uses. It now takes its turn in the one queue
instead of starting beside whatever crawl is running: each verification job
owns a `runs` row of kind `verify`, and the queue orders, moves, removes and
recovers it exactly as it does a crawl.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("kind", sa.String(16), nullable=False, server_default="crawl"),
    )
    op.add_column(
        "verification_jobs",
        sa.Column(
            "run_id", sa.String(64),
            sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.create_index("ix_verification_jobs_run_id", "verification_jobs", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_verification_jobs_run_id", table_name="verification_jobs")
    op.drop_column("verification_jobs", "run_id")
    op.drop_column("runs", "kind")
