"""Resume data survives restarts without rewriting the entire work list."""
import json

from sqlalchemy import func, select, text

from agentscrape.db.models import Run, Site, SiteRun, SiteRunCheckpointPart, SiteRunVisit
from agentscrape.pipeline.checkpoint import (
    apply_checkpoint,
    build_checkpoint,
    checkpoint_parts,
    clear_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from agentscrape.pipeline.state import initial_state


async def seeded(session):
    site = Site(root_domain="checkpoint.edu", canonical_url="https://checkpoint.edu")
    run = Run(config={})
    session.add_all([site, run])
    await session.flush()
    site_run = SiteRun(site_id=site.id, run_id=run.id)
    session.add(site_run)
    await session.flush()
    state = initial_state(site_id=site.id, site_run_id=site_run.id,
                          root_url=site.canonical_url, root_domain=site.root_domain)
    state["candidates"] = [{"url": f"https://checkpoint.edu/residents/{i}", "priority": i}
                           for i in range(300)]
    state["seen_record_ids"] = [f"record-{i}" for i in range(256)]
    return site_run, state


async def physical_rows(session, site_run_id):
    return dict((await session.execute(text(
        "SELECT field || ':' || chunk, ctid::text FROM site_run_checkpoint_parts WHERE site_run_id = :id"
    ), {"id": site_run_id})).all())


async def test_roundtrip_and_only_changed_chunks_are_written(session):
    site_run, state = await seeded(session)
    await save_checkpoint(session, state)
    await session.commit()
    before = await physical_rows(session, site_run.id)
    assert await load_checkpoint(session, site_run.id) == build_checkpoint(state)
    state["cursor"] = 4
    state["seen_record_ids"].append("record-added")
    await save_checkpoint(session, state)
    await session.commit()
    after = await physical_rows(session, site_run.id)
    assert all(after[key] == address for key, address in before.items())
    assert set(after) - set(before) == {"seen_record_ids:2"}
    restored = apply_checkpoint(initial_state(site_id=state['site_id'], site_run_id=site_run.id,
                                root_url=state['root_url'], root_domain=state['root_domain']),
                                await load_checkpoint(session, site_run.id))
    assert restored["cursor"] == 4
    assert restored["seen_record_ids"][-1] == "record-added"
    assert restored["candidates"] == state["candidates"]


async def test_legacy_checkpoint_converts_and_directory_drops_finished_frontier(session):
    site_run, state = await seeded(session)
    site_run.checkpoint_state = build_checkpoint(state)
    await session.commit()
    assert (await load_checkpoint(session, site_run.id))["candidates"] == state["candidates"]
    state["crawl_done"] = True
    await save_checkpoint(session, state)
    await session.commit()
    loaded = await load_checkpoint(session, site_run.id)
    assert loaded["candidates"] == []
    assert loaded["seen_record_ids"] == state["seen_record_ids"]
    assert not await session.scalar(select(func.count()).select_from(SiteRunCheckpointPart).where(
        SiteRunCheckpointPart.site_run_id == site_run.id, SiteRunCheckpointPart.field == "candidates"))


async def test_rollback_keeps_previous_checkpoint_and_finalization_releases_scratch(session):
    site_run, state = await seeded(session)
    site_run_id = site_run.id
    await save_checkpoint(session, state)
    session.add(SiteRunVisit(site_run_id=site_run_id, url="https://checkpoint.edu", url_hash="a" * 64))
    await session.commit()
    state["cursor"] = 8
    state["candidates"] = []
    await save_checkpoint(session, state)
    await session.rollback()
    loaded = await load_checkpoint(session, site_run_id)
    assert loaded["cursor"] == 0 and len(loaded["candidates"]) == 300
    await clear_checkpoint(session, site_run_id)
    await session.commit()
    assert await load_checkpoint(session, site_run_id) is None
    assert not await session.scalar(select(func.count()).select_from(SiteRunCheckpointPart))
    assert not await session.scalar(select(func.count()).select_from(SiteRunVisit))


def test_compression_and_delta_payload_size():
    state = {"candidates": [{"url": f"https://school.edu/residency/{i}", "priority": 50,
                            "context": "Current residents in the department"} for i in range(4096)],
             "seen_record_ids": [f"record-{i}" for i in range(4096)], "cursor": 0}
    manifest, parts = checkpoint_parts(state)
    raw_size = len(json.dumps(build_checkpoint(state)).encode())
    assert sum(len(p['payload']) for p in parts) + len(json.dumps(manifest)) < raw_size / 4
    state['cursor'] = 4
    next_manifest, next_parts = checkpoint_parts(state)
    assert parts == next_parts
    assert len(json.dumps(next_manifest)) < raw_size / 100
