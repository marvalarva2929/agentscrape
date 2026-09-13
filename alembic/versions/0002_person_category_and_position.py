"""Collect everyone on a site, labelled with their position

Scope inverted: the extractor used to keep only residents, fellows and
unclassifiable people and discard faculty, program directors, coordinators,
staff, students and alumni. It now keeps everyone and records what they are.

`role` (resident/fellow/unknown) becomes `category` with a wider vocabulary, and
`position` holds the title exactly as the page printed it.

`pgy_source` and `class_of_source` are dropped: they existed to mark values the
backend inferred, and inference has been removed. What is stored is now always
what the page stated.

Revision ID: 0002_person_category
Revises: edea3953720a
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_person_category"
down_revision = "edea3953720a"
branch_labels = None
depends_on = None

# These enum columns are plain VARCHARs: SQLAlchemy 2.0 defaults
# `create_constraint=False` on Enum, so there is no CHECK constraint to swap.
# The vocabulary is enforced in Python by `db.enums.PersonCategory`.


def upgrade() -> None:
    op.add_column("records", sa.Column("position", sa.Text(), nullable=True))
    op.alter_column("records", "role", new_column_name="category")

    op.drop_index("ix_records_role", table_name="records")
    op.create_index("ix_records_category", "records", ["category"])

    # These marked values the backend inferred; inference has been removed.
    op.drop_column("records", "pgy_source")
    op.drop_column("records", "class_of_source")


def downgrade() -> None:
    op.add_column(
        "records", sa.Column("class_of_source", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "records", sa.Column("pgy_source", sa.String(length=20), nullable=True)
    )

    op.drop_index("ix_records_category", table_name="records")
    # Anyone outside the original three roles has no representation going back.
    op.execute(
        "UPDATE records SET category = 'unknown' "
        "WHERE category NOT IN ('resident', 'fellow', 'unknown')"
    )
    op.alter_column("records", "category", new_column_name="role")
    op.create_index("ix_records_role", "records", ["role"])

    op.drop_column("records", "position")
