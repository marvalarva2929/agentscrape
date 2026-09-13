from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from agentscrape.config import settings
from agentscrape.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Indexes created with raw SQL because SQLAlchemy cannot express them portably:
# a functional GIN index over to_tsvector, and two pg_trgm indexes. They are not
# in the model metadata, so autogenerate would otherwise emit DROP INDEX for
# each of them on the next revision.
MANUALLY_MANAGED_INDEXES = {
    "ix_records_fts",
    "ix_records_name_trgm",
    "ix_sites_domain_trgm",
}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "index" and name in MANUALLY_MANAGED_INDEXES:
        return False
    return True



def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(settings.database_url, poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
