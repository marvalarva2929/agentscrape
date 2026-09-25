"""Preserve the legacy revision recorded by deployed databases.

Revision ID: f3e4d5c6b7a8
Revises: a2b3c4d5e6f

The original script for this deployed revision was not retained in source
control.  It must remain addressable so Alembic can load databases that were
stamped with it.  Schema compatibility is enforced by the successor revision.
"""

from __future__ import annotations

revision = "f3e4d5c6b7a8"
down_revision = "a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
