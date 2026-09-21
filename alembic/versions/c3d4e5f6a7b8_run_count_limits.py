"""run count limits: residents & fellows and email stop reasons."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b7c1d2e3f4a5"
branch_labels = None
depends_on = None

_OLD = ("max_records", "max_spend", "cancelled", "run_timeout")
_NEW = ("max_records", "max_trainees", "max_emails", "max_spend", "cancelled", "run_timeout")


def upgrade() -> None:
    op.alter_column("runs", "stop_reason", existing_type=sa.Enum(*_OLD, name="stop_reason", native_enum=False), type_=sa.Enum(*_NEW, name="stop_reason", native_enum=False), existing_nullable=True)


def downgrade() -> None:
    op.alter_column("runs", "stop_reason", existing_type=sa.Enum(*_NEW, name="stop_reason", native_enum=False), type_=sa.Enum(*_OLD, name="stop_reason", native_enum=False), existing_nullable=True)
