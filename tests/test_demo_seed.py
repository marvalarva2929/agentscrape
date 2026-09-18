"""A fresh deployment opens on real results, not an empty school list.

A coworker deployed the API to an empty database and the UI had no school to
select until someone ran `seed-demo` by hand. The API now loads the snapshots in
demo/ itself when it starts on an empty database, and never on a populated one.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from agentscrape.config import settings
from agentscrape.db.models import Record, Site
from agentscrape.db.session import dispose_engine, session_scope
from agentscrape.demo import seed_if_empty

SNAPSHOT = {
    "version": 1,
    "school": {
        "root_domain": "medicine.example-az.edu",
        "canonical_url": "https://medicine.example-az.edu/",
        "name": "Example College of Medicine",
        "hospital_name": None,
        "location": "Tucson, AZ",
        "institution_type": "university",
    },
    "people": [
        {
            "full_name": name, "email": email, "category": category,
            "position": None, "pgy": None, "class_of": None, "specialty": "Surgery",
            "confidence": 0.85, "source_url": "https://medicine.example-az.edu/surgery/residents",
            "page_title": "Current Residents", "extraction_method": "discovery",
            "fetch_mode": "html", "captured_at": "2026-09-17T23:00:00+00:00",
        }
        for name, email, category in [
            ("Ana Ruiz", "aruiz@example-az.edu", "resident"),
            ("Ben Cole", None, "fellow"),
        ]
    ],
}


@pytest.mark.asyncio
async def test_an_empty_database_is_seeded_once(engine, tmp_path, monkeypatch) -> None:
    (tmp_path / "school.json").write_text(json.dumps(SNAPSHOT))
    monkeypatch.setattr(settings, "demo_snapshot_dir", str(tmp_path))
    try:
        assert await seed_if_empty() == 2
        # A populated database is left alone.
        assert await seed_if_empty() == 0
        async with session_scope() as session:
            assert await session.scalar(select(func.count(Site.id))) == 1
            assert await session.scalar(select(func.count(Record.id))) == 2
    finally:
        await dispose_engine()


@pytest.mark.asyncio
async def test_no_snapshots_leaves_the_database_empty(engine, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "demo_snapshot_dir", str(tmp_path))
    try:
        assert await seed_if_empty() == 0
    finally:
        await dispose_engine()
