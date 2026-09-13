"""Live token/spend metering.

Spend must be observable while a run is in flight, not computed at the end, so
every model call funnels through here and the totals are flushed to the run row
as they happen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..config import settings


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * settings.llm_price_input_per_mtok
            + self.output_tokens / 1_000_000 * settings.llm_price_output_per_mtok
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class UsageMeter:
    """Accumulates usage for one scope (a SiteRun) and reports upward.

    `on_usage` is how the orchestrator learns about spend in real time; it is
    invoked for every call so hard-stop checks see fresh numbers.
    """

    scope: str = "global"
    total: Usage = field(default_factory=Usage)
    calls: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    on_usage: object = None  # async callable(Usage) -> None

    async def record(self, usage: Usage) -> Usage:
        async with self._lock:
            self.total = self.total + usage
            self.calls += 1
            snapshot = self.total
        if self.on_usage is not None:
            await self.on_usage(usage)  # type: ignore[operator]
        return snapshot

    @property
    def cost_usd(self) -> float:
        return self.total.cost_usd
