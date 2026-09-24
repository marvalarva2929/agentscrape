"""Merge verification and checkpoint migration heads.

Revision ID: f1e2d3c4b5a6
Revises: 18b9c0d1e2f3, d0e1f2a3b4c5
Create Date: 2026-09-24 20:58:00.000000

The two branches modify independent database objects.  This no-op revision
only records that both are required before subsequent upgrades can continue.
"""

from __future__ import annotations

revision = "f1e2d3c4b5a6"
down_revision = ("18b9c0d1e2f3", "d0e1f2a3b4c5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
