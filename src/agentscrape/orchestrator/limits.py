"""Hard stops and the memory ceiling.

The client pays compute directly, so a run must be boundable before it starts.
Both limits are optional and per run:
  * maximum records collected
  * maximum estimated spend

When either trips the run stops cleanly: no new sites are claimed, in-flight
sites finish their current step, the queue drains, and the run is marked
`stopped_at_limit` rather than completed or failed. Partial results are valid.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..config import settings
from ..db.enums import StopReason

log = logging.getLogger("agentscrape.limits")


@dataclass
class RunLimits:
    """Live counters for one run, shared by every worker."""

    max_records: int | None = None
    max_spend_usd: float | None = None

    records_collected: int = 0
    spend_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    stop_reason: StopReason | None = None
    cancelled: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def add_records(self, count: int) -> None:
        if count <= 0:
            return
        async with self._lock:
            self.records_collected += count
            self._check()

    async def add_usage(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        async with self._lock:
            self.tokens_in += input_tokens
            self.tokens_out += output_tokens
            self.spend_usd += cost
            self._check()

    def _check(self) -> None:
        if self.stop_reason is not None:
            return
        if self.max_records is not None and self.records_collected >= self.max_records:
            self.stop_reason = StopReason.MAX_RECORDS
            log.info(
                "run hit its record limit (%d/%d); winding down",
                self.records_collected, self.max_records,
            )
        elif self.max_spend_usd is not None and self.spend_usd >= self.max_spend_usd:
            self.stop_reason = StopReason.MAX_SPEND
            log.info(
                "run hit its spend limit ($%.4f/$%.2f); winding down",
                self.spend_usd, self.max_spend_usd,
            )

    def cancel(self) -> None:
        self.cancelled = True
        if self.stop_reason is None:
            self.stop_reason = StopReason.CANCELLED

    @property
    def should_stop(self) -> bool:
        return self.cancelled or self.stop_reason is not None

    @property
    def stopped_at_limit(self) -> bool:
        return self.stop_reason in (StopReason.MAX_RECORDS, StopReason.MAX_SPEND)

    def snapshot(self) -> dict[str, object]:
        return {
            "records_collected": self.records_collected,
            "spend_usd": round(self.spend_usd, 6),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "max_records": self.max_records,
            "max_spend_usd": self.max_spend_usd,
            "stop_reason": str(self.stop_reason) if self.stop_reason else None,
        }


class MemoryCeilingExceeded(RuntimeError):
    """Raised before a run starts when the requested concurrency will not fit."""


def check_memory_ceiling(concurrency: int) -> dict[str, float]:
    """Fail loudly rather than letting concurrent Chromium instances thrash the box.

    Memory is the practical constraint on a single server, so the ceiling is
    explicit and checked before a run is accepted, not discovered at runtime.
    """
    import psutil

    if concurrency > settings.max_concurrent_contexts:
        raise MemoryCeilingExceeded(
            f"Requested concurrency {concurrency} exceeds MAX_CONCURRENT_CONTEXTS "
            f"({settings.max_concurrent_contexts})."
        )

    available_mb = psutil.virtual_memory().available / (1024 * 1024)
    budget_mb = available_mb * settings.memory_safety_factor
    required_mb = concurrency * settings.estimated_mb_per_context

    if required_mb > budget_mb:
        max_fit = max(1, int(budget_mb // settings.estimated_mb_per_context))
        raise MemoryCeilingExceeded(
            f"Concurrency {concurrency} needs about {required_mb:.0f}MB of browser "
            f"memory but only {budget_mb:.0f}MB is safely available "
            f"({available_mb:.0f}MB free x {settings.memory_safety_factor}). "
            f"Use concurrency {max_fit} or fewer."
        )

    return {
        "available_mb": round(available_mb, 1),
        "budget_mb": round(budget_mb, 1),
        "required_mb": round(required_mb, 1),
        "per_context_mb": settings.estimated_mb_per_context,
    }
