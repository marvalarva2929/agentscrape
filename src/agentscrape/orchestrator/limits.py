"""Hard stops and the memory ceiling.

The client pays compute directly, so a run must be boundable before it starts.
Every limit is optional and per run:
  * people collected, residents and fellows collected, people with an email
  * estimated model spend

The three count limits end the *crawl*: no new school is claimed and a school
in progress stops reading pages, but a directory search the run asked for
still looks up the people already found (it is bounded by the spend limit and
its own lookup cap). The spend limit, a cancel and the run timeout stop
everything. Either way the run is marked `stopped_at_limit` rather than
completed or failed, and partial results are valid.

Counts are unique people seen during this run, reported by each school after
every batch of pages, so a limit trips while a school is still being crawled
rather than when it finishes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..config import settings
from ..db.enums import StopReason

log = logging.getLogger("agentscrape.limits")

COUNT_LIMITS = (StopReason.MAX_RECORDS, StopReason.MAX_TRAINEES, StopReason.MAX_EMAILS)
HARD_STOPS = (StopReason.MAX_SPEND, StopReason.CANCELLED, StopReason.RUN_TIMEOUT)


@dataclass(frozen=True)
class SiteCounts:
    people: int = 0
    trainees: int = 0
    emails: int = 0


@dataclass
class RunLimits:
    """Live counters for one run, shared by every worker."""

    max_records: int | None = None
    max_spend_usd: float | None = None
    max_trainees: int | None = None
    max_emails: int | None = None

    spend_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    stop_reason: StopReason | None = None
    cancelled: bool = False
    # The latest counts each school reported, by site run.
    _sites: dict[str, SiteCounts] = field(default_factory=dict, repr=False)
    # Counts from schools that finished without reporting (e.g. skipped).
    _extra_people: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def records_collected(self) -> int:
        return sum(c.people for c in self._sites.values()) + self._extra_people

    @property
    def trainees_collected(self) -> int:
        return sum(c.trainees for c in self._sites.values())

    @property
    def emails_collected(self) -> int:
        return sum(c.emails for c in self._sites.values())

    async def report_counts(self, site_run_id: str, counts: SiteCounts) -> None:
        """A school's running totals for this run (replaces its last report)."""
        async with self._lock:
            self._sites[site_run_id] = counts
            self._check()

    async def add_records(self, count: int) -> None:
        """People from a school that never reported counts of its own."""
        if count <= 0:
            return
        async with self._lock:
            self._extra_people += count
            self._check()

    def has_reported(self, site_run_id: str) -> bool:
        return site_run_id in self._sites

    async def add_usage(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        async with self._lock:
            self.tokens_in += input_tokens
            self.tokens_out += output_tokens
            self.spend_usd += cost
            self._check()

    def _check(self) -> None:
        if self.stop_reason is not None:
            return
        checks = (
            (StopReason.MAX_SPEND, self.max_spend_usd, self.spend_usd, "spend"),
            (StopReason.MAX_RECORDS, self.max_records, self.records_collected, "people"),
            (StopReason.MAX_TRAINEES, self.max_trainees, self.trainees_collected,
             "residents and fellows"),
            (StopReason.MAX_EMAILS, self.max_emails, self.emails_collected, "email"),
        )
        for reason, limit, value, what in checks:
            if limit is not None and value >= limit:
                self.stop_reason = reason
                log.info("run hit its %s limit (%s/%s); winding down", what, value, limit)
                return

    def cancel(self) -> None:
        self.cancelled = True
        if self.stop_reason is None or self.stop_reason in COUNT_LIMITS:
            self.stop_reason = StopReason.CANCELLED

    @property
    def should_stop(self) -> bool:
        """Stop everything now: spend limit, cancel or run timeout."""
        return self.cancelled or self.stop_reason in HARD_STOPS

    @property
    def crawl_limit_reached(self) -> bool:
        """Enough people collected: stop crawling (a directory search may still run)."""
        return self.stop_reason in COUNT_LIMITS

    @property
    def stopped_at_limit(self) -> bool:
        return self.stop_reason in (*COUNT_LIMITS, StopReason.MAX_SPEND)

    def snapshot(self) -> dict[str, object]:
        return {
            "records_collected": self.records_collected,
            "trainees_collected": self.trainees_collected,
            "emails_collected": self.emails_collected,
            "spend_usd": round(self.spend_usd, 6),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "max_records": self.max_records,
            "max_trainees": self.max_trainees,
            "max_emails": self.max_emails,
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
