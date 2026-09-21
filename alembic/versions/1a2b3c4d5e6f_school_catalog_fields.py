"""Add non-destructive active school catalog fields."""

from alembic import op
import sqlalchemy as sa

revision = "1a2b3c4d5e6f"
down_revision = "9e36696d05e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("directory_url", sa.Text(), nullable=True))
    op.add_column("sites", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.alter_column("sites", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_column("sites", "is_active")
    op.drop_column("sites", "directory_url")
