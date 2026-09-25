"""merge verification and index branches

Revision ID: f2a3b4c5d6e7
Revises: 18b9c0d1e2f3, e1f2a3b4c5d6
Create Date: 2026-09-25

This is an empty merge revision.  It preserves both historical branches and
makes upgrades converge on one Alembic head.
"""

revision = "f2a3b4c5d6e7"
down_revision = ("18b9c0d1e2f3", "e1f2a3b4c5d6")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
