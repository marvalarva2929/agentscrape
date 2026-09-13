"""Per-domain token bucket.

robots.txt Disallow is not enforced (see RESPECT_ROBOTS in config) — this limiter
and the identifying User-Agent are what keep the crawl polite, so it is always on
and applies to browser navigations as well as plain fetches.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from ..config import settings
from ..urls import registrable_domain


class DomainRateLimiter:
    """One bucket per registrable domain, shared across all agents in a process.

    Bucketing by registrable domain rather than hostname is deliberate: twenty
    departmental subdomains of one university are still one institution's server.
    """

    def __init__(self, requests_per_second: float | None = None) -> None:
        self.rate = requests_per_second or settings.requests_per_second_per_domain
        self._next_allowed: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, host: str) -> None:
        if self.rate <= 0:
            return
        bucket = registrable_domain(host)
        interval = 1.0 / self.rate
        async with self._locks[bucket]:
            now = time.monotonic()
            wait = self._next_allowed[bucket] - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed[bucket] = now + interval

    def note_crawl_delay(self, host: str, delay_seconds: float) -> None:
        """Honour a site-declared Crawl-delay when it is stricter than our rate."""
        if delay_seconds <= 0:
            return
        bucket = registrable_domain(host)
        implied_rate = 1.0 / delay_seconds
        if implied_rate < self.rate:
            self._next_allowed[bucket] = max(
                self._next_allowed[bucket], time.monotonic() + delay_seconds
            )


_limiter: DomainRateLimiter | None = None


def get_rate_limiter() -> DomainRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = DomainRateLimiter()
    return _limiter
