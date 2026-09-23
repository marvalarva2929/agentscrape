"""Live token/spend metering.

Spend must be observable while a run is in flight, not computed at the end, so
every model call funnels through here and the totals are flushed to the run row
as they happen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..config import settings


def price_per_mtok(model: str) -> tuple[float, float]:
    """(input, output) USD per million tokens for the model that served a call.

    Only the cheap triage model is priced separately; every other model is
    billed at the main rate, as before.
    """
    cheap = settings.llm_cheap_model
    if cheap and model == cheap and cheap != settings.text_model:
        return settings.llm_cheap_price_input_per_mtok, settings.llm_cheap_price_output_per_mtok
    return settings.llm_price_input_per_mtok, settings.llm_price_output_per_mtok


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    # The model that served the call, for pricing. Blank on a sum of calls,
    # which carries its cost in `usd` instead.
    model: str = ""
    usd: float | None = None

    @property
    def cost_usd(self) -> float:
        if self.usd is not None:
            return self.usd
        price_in, price_out = price_per_mtok(self.model)
        return (
            self.input_tokens / 1_000_000 * price_in
            + self.output_tokens / 1_000_000 * price_out
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            usd=self.cost_usd + other.cost_usd,
        )


class LLMUnavailable(RuntimeError):
    """Too many model calls in a row failed; the site cannot be read by the agent."""


@dataclass
class UsageMeter:
    """Accumulates usage for one scope (a SiteRun) and reports upward.

    `on_usage` is how the orchestrator learns about spend in real time; it is
    invoked for every call so hard-stop checks see fresh numbers.
    """

    scope: str = "global"
    total: Usage = field(default_factory=Usage)
    calls: int = 0
    # Every failed model call, and the current run of them. A success resets
    # the run; too long a run means the endpoint is down, not that one page
    # was awkward.
    failures: int = 0
    consecutive_failures: int = 0
    # What the most recent failure was, for a report that says why.
    last_failure: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    on_usage: object = None  # async callable(Usage) -> None

    async def record(self, usage: Usage) -> Usage:
        async with self._lock:
            self.total = self.total + usage
            self.calls += 1
            self.consecutive_failures = 0
            snapshot = self.total
        if self.on_usage is not None:
            await self.on_usage(usage)  # type: ignore[operator]
        return snapshot

    @property
    def cost_usd(self) -> float:
        return self.total.cost_usd

    def note_failure(self, what: str, exc: BaseException | str) -> None:
        """Count a failed model call; raise once failures stop being isolated."""
        self.failures += 1
        self.consecutive_failures += 1
        self.last_failure = f"{what}: {exc}"[:300]
        limit = settings.llm_max_consecutive_failures
        if limit and self.consecutive_failures >= limit:
            raise LLMUnavailable(
                f"{self.consecutive_failures} model calls failed in a row "
                f"(last: {what}: {exc})"
            )
