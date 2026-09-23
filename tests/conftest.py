"""Integration fixtures backed by a real Postgres database.

Reconciliation depends on Postgres-specific behaviour (advisory locks, JSONB,
SKIP LOCKED), so these run against the real engine rather than SQLite.

Schema creation uses a synchronous engine once per session; each test then gets
its own async engine. Sharing one async engine across tests fails because
pytest-asyncio gives each test a fresh event loop and asyncpg connections are
bound to the loop that created them.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://agentscrape:agentscrape@localhost:5432/agentscrape_test",
)
SYNC_DATABASE_URL = TEST_DATABASE_URL.replace("+asyncpg", "+psycopg")

# Must be set before anything reads settings.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Tests control their own data; the startup seed is tested explicitly.
os.environ["SEED_DEMO_ON_STARTUP"] = "false"

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from agentscrape.db.models import Base  # noqa: E402


class OfflineProvider:
    """Stands in for the model endpoint: every call fails at once, so tests
    exercise the heuristic fallbacks and never touch the network. Tests about
    model behaviour install their own fake with `set_provider`."""

    async def complete(self, **kwargs):
        raise ConnectionError("model endpoint is offline in tests")


@pytest.fixture(autouse=True)
def offline_model(monkeypatch):
    from agentscrape.config import settings
    from agentscrape.llm import provider

    monkeypatch.setattr(provider, "_provider", OfflineProvider())
    # Every call fails here by design; the abort is tested explicitly.
    monkeypatch.setattr(settings, "llm_max_consecutive_failures", 0)
    # A finished crawl would otherwise queue a verification pass that outlives
    # the test; verification is tested on its own.
    monkeypatch.setattr(settings, "auto_verify_after_crawl", False)
    yield


@pytest.fixture(scope="session")
def schema():
    """Create the schema once for the whole test session."""
    engine = create_engine(SYNC_DATABASE_URL, future=True)
    with engine.begin() as connection:
        Base.metadata.drop_all(connection)
        Base.metadata.create_all(connection)
    engine.dispose()
    yield


@pytest.fixture
def clean_tables(schema):
    """Truncate before each test, synchronously, so no async loop is involved."""
    engine = create_engine(SYNC_DATABASE_URL, future=True)
    with engine.begin() as connection:
        tables = ", ".join(Base.metadata.tables)
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    engine.dispose()
    yield


@pytest_asyncio.fixture
async def engine(clean_tables):
    engine = create_async_engine(TEST_DATABASE_URL, future=True, poolclass=None)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine, reset_global_engine):
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def reset_global_engine():
    """Give every test a fresh application engine.

    `agentscrape.db.session` memoizes one async engine per process, and asyncpg
    connections are bound to the event loop that created them. pytest-asyncio
    hands each test a new loop, so a cached engine from an earlier test fails
    with "attached to a different loop". Disposing around each test keeps the
    application code (which rightly uses a singleton) unchanged.
    """
    from agentscrape.db import session as session_module

    await session_module.dispose_engine()
    yield
    await session_module.dispose_engine()
