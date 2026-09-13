"""Runtime dependencies for the per-site pipeline.

These are the non-serializable things the graph needs (browser context, HTTP
client, session factory, event emitter). They are bound at graph-construction
time via closure rather than carried in the checkpointed state.
"""

from __future__ import annotations

from dataclasses import dataclass
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
