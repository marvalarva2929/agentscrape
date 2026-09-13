"""Mid-site checkpointing.

The server is stopped on demand, so a site can be interrupted anywhere. The
visited-URL set alone would make a resumed site correct but not cheap: it would
re-run discovery, which is by far the most expensive stage.

So the ranked candidate list and the loop's position are persisted to
`site_runs.checkpoint_state` after every extract batch. A resumed site restores
them and goes straight back to extracting.

Only JSON-serializable progress is stored. Records themselves are already in the
database — reconciliation happens per batch precisely so an interrupted site
keeps everything it collected.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import SiteRun
from .state import SiteState

log = logging.getLogger("agentscrape.checkpoint")

CHECKPOINT_VERSION = 1

# Fields worth restoring. Everything else is either configuration (supplied
# fresh on resume) or derivable.
CHECKPOINTED_FIELDS = (
    "candidates", "cursor", "steps_taken", "candidates_considered",
    "known_path_hits", "records_new", "records_changed", "records_unchanged",
    "seen_record_ids", "fingerprint", "dominant_specialty", "similarity_score",
)


def build_checkpoint(state: SiteState) -> dict[str, Any]:
    return {
        "version": CHECKPOINT_VERSION,
        **{key: state.get(key) for key in CHECKPOINTED_FIELDS},
    }


async def save_checkpoint(session: AsyncSession, state: SiteState) -> None:
    await session.execute(
        update(SiteRun)
        .where(SiteRun.id == state["site_run_id"])
        .values(checkpoint_state=build_checkpoint(state), steps_taken=state.get("steps_taken", 0))
    )


async def load_checkpoint(
    session: AsyncSession, site_run_id: str
) -> dict[str, Any] | None:
    """Return a usable checkpoint, or None to start the site from scratch."""
    stored = await session.scalar(
        select(SiteRun.checkpoint_state).where(SiteRun.id == site_run_id)
    )
    if not stored or not isinstance(stored, dict):
        return None
    if stored.get("version") != CHECKPOINT_VERSION:
        # A checkpoint from an older shape is discarded rather than guessed at.
        log.info("discarding checkpoint with version %r", stored.get("version"))
        return None
    if not stored.get("candidates"):
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
    """Drop the checkpoint once a site reaches a terminal state."""
    await session.execute(
        update(SiteRun).where(SiteRun.id == site_run_id).values(checkpoint_state=None)
    )
