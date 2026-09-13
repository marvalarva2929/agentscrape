"""Migrations must produce exactly the schema the models describe.

The rest of the suite builds its schema with `Base.metadata.create_all`, which
is fast but never executes a migration. That gap let a real bug through: two
columns were added to a model with no matching migration, so every test passed
while the first live run failed on INSERT. This test closes it by upgrading a
scratch database through the real migration chain and diffing the result against
the models.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from agentscrape.db.models import Base
from alembic import command

from .conftest import SYNC_DATABASE_URL

MIGRATION_DB = "agentscrape_migrations"

# Objects the models deliberately do not describe: functional GIN indexes
# created with raw SQL, and Postgres' implicit SERIAL sequence.
IGNORED = ("ix_records_fts", "ix_records_name_trgm", "ix_sites_domain_trgm")


def _admin_url() -> str:
    return SYNC_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"


def _scratch_url() -> str:
    return SYNC_DATABASE_URL.rsplit("/", 1)[0] + f"/{MIGRATION_DB}"


@pytest.fixture(scope="module")
def migrated_db():
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB}"))
        connection.execute(text(f"CREATE DATABASE {MIGRATION_DB}"))
    admin.dispose()

    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", _scratch_url().replace("+psycopg", "+asyncpg")
    )
    try:
        command.upgrade(config, "head")
        yield _scratch_url()
    finally:
        admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB}"))
        admin.dispose()


def _meaningful(diffs) -> list:
    kept = []
    for diff in diffs:
        entry = diff[0] if isinstance(diff, list) else diff
        name = ""
        if isinstance(entry, tuple) and len(entry) > 1:
            obj = entry[-1]
            name = getattr(obj, "name", "") or ""
        if any(ignored in str(name) for ignored in IGNORED):
            continue
        kept.append(diff)
    return kept


class TestMigrationsMatchModels:
    def test_upgrade_head_produces_the_model_schema(self, migrated_db):
        engine = create_engine(migrated_db)
        try:
            with engine.connect() as connection:
                context = MigrationContext.configure(connection)
                diffs = _meaningful(compare_metadata(context, Base.metadata))
        finally:
            engine.dispose()

        assert not diffs, (
            "The migration chain and the models disagree. Run "
            "`alembic revision --autogenerate` and commit the result.\n"
            f"Differences: {diffs}"
        )

    def test_every_model_table_exists_after_upgrade(self, migrated_db):
        engine = create_engine(migrated_db)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                ).scalars().all()
        finally:
            engine.dispose()

        missing = set(Base.metadata.tables) - set(rows)
        assert not missing, f"tables missing after migration: {sorted(missing)}"
