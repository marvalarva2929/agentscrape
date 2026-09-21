"""site directory url, learned search config and affiliated domains."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b7c1d2e3f4a5"
down_revision = "9e36696d05e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("directory_url", sa.Text(), nullable=True))
    op.add_column("sites", sa.Column("directory_config", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=True))
    op.add_column("sites", sa.Column("affiliated_domains", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "affiliated_domains")
    op.drop_column("sites", "directory_config")
    op.drop_column("sites", "directory_url")
