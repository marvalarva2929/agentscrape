"""Runtime dependencies for the per-site pipeline.

These are the non-serializable things the graph needs (browser context, HTTP
client, session factory, event emitter). They are bound at graph-construction
time via closure rather than carried in the checkpointed state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..browser.fetcher import Fetcher
from ..llm.usage import UsageMeter
from ..orchestrator.events import EventEmitter, NullEmitter

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass
class PipelineDeps:
    fetcher: Fetcher
    sessionmaker: async_sessionmaker[AsyncSession]
    browser_context: BrowserContext | None = None
    emitter: EventEmitter | None = None
    meter: UsageMeter | None = None
    # Returns True when the run has hit a hard stop and work should wind down.
    should_stop: object = None  # callable() -> bool
    # Hybrid strategy: bodies the HTML pass already fetched, by candidate URL,
    # so the model phase reads them without fetching again. Not checkpointed.
    page_cache: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.emitter is None:
            self.emitter = NullEmitter()
        if self.meter is None:
            self.meter = UsageMeter()

    @property
    def can_render(self) -> bool:
        return self.browser_context is not None

    def stop_requested(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    async def note(self, state: dict, message: str, **extra) -> None:
        """A human-readable line for the live activity feed."""
        from ..orchestrator.events import EventType

        await self.emitter.emit(
            EventType.SITE_STEP,
            site_id=state.get("site_id"),
            site_run_id=state.get("site_run_id"),
            domain=state.get("root_domain"),
            action="note",
            message=message,
            **extra,
        )

    async def emit_stage(self, state: dict, stage: str) -> None:
        """Tell the monitor roughly where this site has got to."""
        from ..orchestrator.events import EventType

        candidates = len(state.get("candidates") or [])
        cursor = int(state.get("cursor") or 0)
        # Progress is the share of the ranked candidate list worked through;
        # discovery counts as the first slice so the bar is never stuck at zero.
        if stage == "discovering":
            progress = 10
        elif stage == "finalizing":
            progress = 95
        elif stage == "complete":
            progress = 100
        elif candidates:
            progress = 10 + int(min(cursor / candidates, 1.0) * 80)
        else:
            progress = 10

        await self.emitter.emit(
            EventType.SITE_STEP,
            site_id=state.get("site_id"),
            site_run_id=state.get("site_run_id"),
            domain=state.get("root_domain"),
            stage=stage,
            progress=progress,
            records_found=(
                state.get("records_new", 0)
                + state.get("records_changed", 0)
                + state.get("records_unchanged", 0)
            ),
        )
