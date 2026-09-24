"""Mid-site checkpointing.

The server is stopped on demand, so a site can be interrupted anywhere. The
visited-URL set alone would make a resumed site correct but not cheap: it would
re-run discovery, which is by far the most expensive stage.

The candidate list and progress are saved after every batch. Large lists live
in compressed chunks in `site_run_checkpoint_parts`; `checkpoint_state` holds
only a small manifest and counters. Identical chunks are not rewritten. A
resumed site restores them and goes straight back to extracting. Legacy inline
checkpoints remain readable and convert on their next save.

Only JSON-serializable progress is stored. Records themselves are already in the
database — reconciliation happens per batch precisely so an interrupted site
keeps everything it collected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from typing import Any

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import SiteRun, SiteRunCheckpointPart, SiteRunVisit
from .state import SiteState

log = logging.getLogger("agentscrape.checkpoint")

CHECKPOINT_VERSION = 2
PARTS_VERSION = 3
CHUNK_SIZE = 128

# Fields worth restoring. Everything else is either configuration (supplied
# fresh on resume) or derivable.
CHECKPOINTED_FIELDS = (
    "candidates", "cursor", "steps_taken", "candidates_considered",
    "known_path_hits", "records_new", "records_changed", "records_unchanged",
    "seen_record_ids", "dominant_specialty",
    "barren_streak", "triaged", "processed_hashes", "programs", "gap_rounds",
    "html_map_stats", "crawl_done", "directory_done", "directory_stats", "http_refusals",
)


def build_checkpoint(state: SiteState) -> dict[str, Any]:
    return {
        "version": CHECKPOINT_VERSION,
        **{key: state.get(key) for key in CHECKPOINTED_FIELDS},
    }


def checkpoint_parts(state: SiteState) -> tuple[dict, list[dict]]:
    """Bound each rewrite to a changed chunk, rather than the whole crawl."""
    checkpoint = build_checkpoint(state)
    # Once crawling is done, only result IDs/counters are needed by directory
    # search and finalization. Never store the finished crawl's frontier again.
    if state.get("crawl_done"):
        for key in ("candidates", "triaged", "processed_hashes"):
            checkpoint[key] = []
        checkpoint["cursor"] = 0
    scalars, lists, parts = {}, {}, []
    for key, value in checkpoint.items():
        if key == "version":
            continue
        if not isinstance(value, list):
            scalars[key] = value
            continue
        lists[key] = (len(value) + CHUNK_SIZE - 1) // CHUNK_SIZE
        for start in range(0, len(value), CHUNK_SIZE):
            raw = json.dumps(value[start:start + CHUNK_SIZE], sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False).encode()
            parts.append({"field": key, "chunk": start // CHUNK_SIZE,
                          "digest": hashlib.sha256(raw).hexdigest(),
                          "payload": zlib.compress(raw)})
    return {"version": PARTS_VERSION, "scalars": scalars, "lists": lists}, parts


async def save_checkpoint(session: AsyncSession, state: SiteState) -> None:
    site_run_id = state["site_run_id"]
    manifest, parts = checkpoint_parts(state)
    # Serialize concurrent saves/clears and keep the manifest atomic with data.
    await session.execute(select(SiteRun.id).where(SiteRun.id == site_run_id).with_for_update())
    existing = {(field, chunk): digest for field, chunk, digest in (await session.execute(
        select(SiteRunCheckpointPart.field, SiteRunCheckpointPart.chunk, SiteRunCheckpointPart.digest)
        .where(SiteRunCheckpointPart.site_run_id == site_run_id)
    )).all()}
    desired = {(part["field"], part["chunk"]) for part in parts}
    changed = [{"site_run_id": site_run_id, **part} for part in parts
               if existing.get((part["field"], part["chunk"])) != part["digest"]]
    # Bound statement size for large institutions.
    for start in range(0, len(changed), 100):
        statement = insert(SiteRunCheckpointPart).values(changed[start:start + 100])
        await session.execute(statement.on_conflict_do_update(
            index_elements=["site_run_id", "field", "chunk"],
            set_={"digest": statement.excluded.digest, "payload": statement.excluded.payload},
        ))
    stale = set(existing) - desired
    if stale:
        await session.execute(delete(SiteRunCheckpointPart).where(
            SiteRunCheckpointPart.site_run_id == site_run_id,
            tuple_(SiteRunCheckpointPart.field, SiteRunCheckpointPart.chunk).in_(stale),
        ))
    await session.execute(
        update(SiteRun).where(SiteRun.id == site_run_id)
        .values(checkpoint_state=manifest, steps_taken=state.get("steps_taken", 0))
    )


async def load_checkpoint(
    session: AsyncSession, site_run_id: str
) -> dict[str, Any] | None:
    """Return a usable checkpoint, or None to start the site from scratch."""
    stored = await session.scalar(
        select(SiteRun.checkpoint_state).where(SiteRun.id == site_run_id).with_for_update()
    )
    if not stored or not isinstance(stored, dict):
        return None
    if stored.get("version") not in (CHECKPOINT_VERSION, PARTS_VERSION):
        # A checkpoint from an older shape is discarded rather than guessed at.
        log.info("discarding checkpoint with version %r", stored.get("version"))
        return None
    if stored.get("version") == PARTS_VERSION:
        restored = {"version": CHECKPOINT_VERSION, **stored["scalars"]}
        rows = (await session.execute(select(SiteRunCheckpointPart)
            .where(SiteRunCheckpointPart.site_run_id == site_run_id)
            .order_by(SiteRunCheckpointPart.field, SiteRunCheckpointPart.chunk))).scalars().all()
        for key, count in stored["lists"].items():
            chunks = [part for part in rows if part.field == key]
            if [part.chunk for part in chunks] != list(range(count)):
                raise ValueError(f"Incomplete checkpoint: {key}")
            restored[key] = []
            for part in chunks:
                raw = zlib.decompress(part.payload)
                if hashlib.sha256(raw).hexdigest() != part.digest:
                    raise ValueError(f"Corrupt checkpoint: {key}")
                restored[key].extend(json.loads(raw))
        stored = restored
    if not stored.get("candidates") and not stored.get("crawl_done"):
        # Nothing useful to resume: discovery had not finished.
        return None
    return stored


def apply_checkpoint(state: SiteState, checkpoint: dict[str, Any]) -> SiteState:
    restored = dict(state)
    for key in CHECKPOINTED_FIELDS:
        if key in checkpoint and checkpoint[key] is not None:
            restored[key] = checkpoint[key]
    restored["resumed"] = True
    log.info(
        "resuming %s at candidate %s/%s (%s steps already spent)",
        state.get("root_domain"),
        restored.get("cursor"),
        len(restored.get("candidates", [])),
        restored.get("steps_taken"),
    )
    return restored  # type: ignore[return-value]


async def clear_checkpoint(session: AsyncSession, site_run_id: str) -> None:
    """Drop scratch state after finalization has consumed the visit set."""
    await session.execute(select(SiteRun.id).where(SiteRun.id == site_run_id).with_for_update())
    await session.execute(delete(SiteRunCheckpointPart).where(
        SiteRunCheckpointPart.site_run_id == site_run_id))
    await session.execute(delete(SiteRunVisit).where(SiteRunVisit.site_run_id == site_run_id))
    await session.execute(
        update(SiteRun).where(SiteRun.id == site_run_id).values(checkpoint_state=None)
    )
