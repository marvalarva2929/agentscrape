"""Stop re-crawls from rewriting every index of every unchanged record.

* `ix_records_last_seen`: a re-sighting bumps `last_seen_at`, and indexing it
  made each of those a non-HOT update (new tuple + an entry in all indexes).
  No query can use it: the people listing orders by a CASE first.
* `ix_records_fts`: its expression (`||`) never matched the query's
  (`concat_ws`), so Postgres could never use it; it was pure write cost.
* `ix_versions_record`: an exact duplicate of `uq_version_record_no`.

Revision ID: 18b9c0d1e2f3
Revises: 07a8b9c0d1e2
"""
from alembic import op

revision = "18b9c0d1e2f3"
down_revision = "07a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("ix_records_last_seen", table_name="records")
    op.execute("DROP INDEX IF EXISTS ix_records_fts")
    op.drop_index("ix_versions_record", table_name="record_versions")


def downgrade():
    op.create_index("ix_versions_record", "record_versions", ["record_id", "version_no"])
    op.execute(
        """
        CREATE INDEX ix_records_fts ON records USING GIN (
            to_tsvector('simple',
                coalesce(full_name, '') || ' ' || coalesce(email, '') || ' ' ||
                coalesce(specialty_normalized, '') || ' ' || coalesce(specialty_raw, ''))
        )
        """
    )
    op.create_index("ix_records_last_seen", "records", ["last_seen_at"])
