"""Legacy merge verification and index migration heads.

Revision ID: e1f2a3b4c5d6
Revises: 18b9c0d1e2f3, d0e1f2a3b4c5
Create Date: 2026-09-24 13:00:00.000000

This revision was deployed before the merge revision was renamed.  It has no
schema changes, but must remain available so databases already stamped with
this revision can load their migration history.
"""

from __future__ import annotations

revision = "e1f2a3b4c5d6"
down_revision = ("18b9c0d1e2f3", "d0e1f2a3b4c5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
