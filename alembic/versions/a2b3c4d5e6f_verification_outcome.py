"""Explicit per-record verification outcome.

Revision ID: a2b3c4d5e6f
Revises: f2e3d4c5b6a7
Create Date: 2026-09-25 11:00:00.000000

Records the result of the most recent verification attempt so unresolved,
unreadable, and technical-failure cases are distinguishable from records that
were never verified.  It follows the deployed-history reconciliation revision;
the original feature branch mistakenly reused its legacy revision ID.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a2b3c4d5e6f"
down_revision = "f2e3d4c5b6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("records", sa.Column("verification_outcome", sa.String(24), nullable=True))
    op.execute(
        """
        UPDATE records
        SET verification_outcome = CASE roles ->> 0
            WHEN 'resident' THEN 'verified_resident'
            WHEN 'fellow' THEN 'verified_fellow'
            ELSE 'verified_non_trainee'
        END
        WHERE roles_checked_at IS NOT NULL
          AND roles IS NOT NULL
          AND jsonb_typeof(roles) = 'array'
          AND jsonb_array_length(roles) = 1
        """
    )


def downgrade() -> None:
    op.drop_column("records", "verification_outcome")
