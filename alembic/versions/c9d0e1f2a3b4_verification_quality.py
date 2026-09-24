"""durable verification quality and evidence

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-24 12:00:00.000000
"""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("records", sa.Column("verification_confidence", sa.Float(), nullable=True))
    op.add_column("records", sa.Column("verification_risk", sa.String(24), nullable=False, server_default="unverified"))
    op.add_column("records", sa.Column("verification_reason", sa.Text(), nullable=True))
    op.add_column("records", sa.Column("verification_evidence", sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column("records", "verification_evidence")
    op.drop_column("records", "verification_reason")
    op.drop_column("records", "verification_risk")
    op.drop_column("records", "verification_confidence")
