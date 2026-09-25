"""explicit per-record verification outcome

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-09-24 15:00:00.000000

Adds `records.verification_outcome`: the result of the most recent
verification attempt (VERIFIED_RESIDENT / VERIFIED_FELLOW /
VERIFIED_NON_TRAINEE / INSUFFICIENT_EVIDENCE / SOURCE_UNAVAILABLE /
VERIFICATION_ERROR), null meaning never attempted. Distinct from
`verification_risk`, which grades the trustworthiness of an already-grounded
decision rather than whether one was reached at all - so a record checked but
left ungrounded no longer looks identical to one nobody has ever verified.

The backfill is conservative: only a record with a single-element `roles`
array and a `roles_checked_at` timestamp - the shape a real confirmed
decision has always been written as - is backfilled, straight from that
value, not guessed. Everything else (all of the historically unresolved
records) is left null.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
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
