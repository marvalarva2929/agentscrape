"""queue order, run heartbeat, school crawl order and crawl names

Revision ID: d4e5f6a7b8c9
Revises: 1a2b3c4d5e6f
Create Date: 2026-09-21 16:00:00.000000

`runs.queued` and the partial unique index make "one queued run at a time" a
property of the database. Existing runs are unqueued and keep their old
behaviour; crawls made before names were defaulted get one from their school.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("queued", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(
        "runs", sa.Column("queue_rank", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "site_runs",
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index(
        "ux_runs_one_running_queued",
        "runs",
        ["queued"],
        unique=True,
        postgresql_where=sa.text("status = 'running' AND queued"),
    )

    # A name for every crawl that has none: the school it crawled, plus how many
    # more it covered, so Past crawls never lists an anonymous row.
    op.execute(
        """
        UPDATE runs SET label = named.label
        FROM (
            SELECT r.id AS id,
                   COALESCE(first_site.name, first_site.hospital_name, first_site.root_domain)
                   || CASE WHEN r.sites_total > 1
                           THEN ' + ' || (r.sites_total - 1) || ' more' ELSE '' END AS label
            FROM runs r
            JOIN LATERAL (
                SELECT s.name, s.hospital_name, s.root_domain
                FROM site_runs sr JOIN sites s ON s.id = sr.site_id
                WHERE sr.run_id = r.id
                ORDER BY sr.created_at, sr.id
                LIMIT 1
            ) first_site ON true
            WHERE r.label IS NULL OR r.label = ''
        ) AS named
        WHERE runs.id = named.id
        """
    )


def downgrade() -> None:
    op.drop_index("ux_runs_one_running_queued", table_name="runs")
    op.drop_column("site_runs", "position")
    op.drop_column("runs", "heartbeat_at")
    op.drop_column("runs", "queue_rank")
    op.drop_column("runs", "queued")
