"""Store crawl checkpoints in compressed, independently updated chunks.

Revision ID: 07a8b9c0d1e2
Revises: b8c9d0e1f2a3
"""
import sqlalchemy as sa

from alembic import op

revision = "07a8b9c0d1e2"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "site_run_checkpoint_parts",
        sa.Column("site_run_id", sa.String(64), sa.ForeignKey("site_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("field", sa.String(64), primary_key=True),
        sa.Column("chunk", sa.Integer(), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
    )


def downgrade():
    # Reassemble resumable state before removing the chunk table.
    import json
    import zlib

    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT id, checkpoint_state FROM site_runs WHERE checkpoint_state->>'version' = '3'"
    )).all()
    for site_run_id, manifest in rows:
        data = {**manifest.get("scalars", {}), **{key: [] for key in manifest.get("lists", {})}}
        parts = connection.execute(sa.text(
            "SELECT field, payload FROM site_run_checkpoint_parts WHERE site_run_id = :id ORDER BY field, chunk"
        ), {"id": site_run_id}).all()
        for field, payload in parts:
            data.setdefault(field, []).extend(json.loads(zlib.decompress(payload)))
        data["version"] = 2
        connection.execute(sa.text(
            "UPDATE site_runs SET checkpoint_state = CAST(:state AS jsonb) WHERE id = :id"
        ), {"state": json.dumps(data), "id": site_run_id})
    op.drop_table("site_run_checkpoint_parts")
