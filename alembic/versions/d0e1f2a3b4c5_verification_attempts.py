"""verification audit attempts

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-24 12:30:00.000000
"""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("verification_attempts", sa.Column("id", sa.String(64), primary_key=True), sa.Column("job_id", sa.String(64), sa.ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False), sa.Column("record_id", sa.String(64), sa.ForeignKey("records.id", ondelete="CASCADE"), nullable=False), sa.Column("stage", sa.String(32), nullable=False), sa.Column("outcome", sa.String(32), nullable=False), sa.Column("source_url", sa.Text()), sa.Column("final_url", sa.Text()), sa.Column("http_status", sa.Integer()), sa.Column("detail", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_verification_attempts_job_record", "verification_attempts", ["job_id", "record_id"])

def downgrade() -> None:
    op.drop_index("ix_verification_attempts_job_record", table_name="verification_attempts")
    op.drop_table("verification_attempts")
