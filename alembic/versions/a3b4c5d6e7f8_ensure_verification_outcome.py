"""Ensure verification outcome support after legacy deployed revision.

Revision ID: a3b4c5d6e7f8
Revises: f3e4d5c6b7a8

Older deployed databases may report the legacy predecessor even when its
source migration is unavailable.  The DDL is deliberately idempotent so both
that history and the current source history converge without data loss.
"""

from __future__ import annotations

from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "f3e4d5c6b7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE records ADD COLUMN IF NOT EXISTS verification_outcome VARCHAR(24)"
    )
    op.execute(
        """
        UPDATE records
        SET verification_outcome = CASE roles ->> 0
            WHEN 'resident' THEN 'verified_resident'
            WHEN 'fellow' THEN 'verified_fellow'
            ELSE 'verified_non_trainee'
        END
        WHERE verification_outcome IS NULL
          AND roles_checked_at IS NOT NULL
          AND roles IS NOT NULL
          AND jsonb_typeof(roles) = 'array'
          AND jsonb_array_length(roles) = 1
        """
    )


def downgrade() -> None:
    pass
