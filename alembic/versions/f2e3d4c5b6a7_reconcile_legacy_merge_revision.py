"""Reconcile legacy and renamed merge revisions.

Revision ID: f2e3d4c5b6a7
Revises: e1f2a3b4c5d6, f1e2d3c4b5a6
Create Date: 2026-09-24 21:15:00.000000

Both predecessors are no-op merge revisions with the same schema ancestry.
Merging them preserves upgrade paths for databases that recorded either ID.
"""

from __future__ import annotations

revision = "f2e3d4c5b6a7"
down_revision = ("e1f2a3b4c5d6", "f1e2d3c4b5a6")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
