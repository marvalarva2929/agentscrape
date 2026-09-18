"""Per-domain token bucket.

robots.txt Disallow is not enforced (see RESPECT_ROBOTS in config) — this limiter
and the identifying User-Agent are what keep the crawl polite, so it is always on
and applies to browser navigations as well as plain fetches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from ..config import settings
from ..urls import registrable_domain

# Never back off past one request every four seconds: slower than this a
# large institution cannot be crawled at all.
_MIN_RATE = 0.25
# A host that refuses this many requests in a row is rate-limiting us; fewer
# than that is just a few pages it will not serve.
_REFUSALS_BEFORE_BACKOFF = 3
# ...and this many clean requests in a row means it has stopped.
_SUCCESSES_BEFORE_RECOVERY = 25

log = logging.getLogger("agentscrape.ratelimit")


class DomainRateLimiter:
    """One bucket per registrable domain, shared across all agents in a process.

    Bucketing by registrable domain rather than hostname is deliberate: twenty
    departmental subdomains of one university are still one institution's server.
    """

    def __init__(self, requests_per_second: float | None = None) -> None:
        self.rate = requests_per_second or settings.requests_per_second_per_domain
        self._next_allowed: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Set per bucket when a host starts refusing us; see note_throttled.
        self._backoff: dict[str, float] = {}
        self._refusals: dict[str, int] = {}
        self._successes: dict[str, int] = {}

    def rate_for(self, host: str) -> float:
        return self._backoff.get(registrable_domain(host), self.rate)

    async def acquire(self, host: str) -> None:
        bucket = registrable_domain(host)
        rate = self._backoff.get(bucket, self.rate)
        if rate <= 0:
            return
        interval = 1.0 / rate
        async with self._locks[bucket]:
            now = time.monotonic()
            wait = self._next_allowed[bucket] - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed[bucket] = now + interval

    def note_throttled(self, host: str) -> None:
        """Slow down for a host that is refusing us — but only if it keeps at it.

        A run of refusals is the site asking us to back off, and carrying on at
        the same pace burns the step budget on refused pages: Baylor Scott &
        White started refusing after about ninety requests and that run ended
        with most of its rosters unvisited.

        A *scattered* refusal is different. BCM returned three 403s among 980
        successful requests — a few pages it will not serve, not a rate limit —
        and halving on each of those dropped the crawl to the floor rate and made
        it four times slower for nothing. So the trigger is consecutive refusals,
        and `note_success` undoes it.
        """
        bucket = registrable_domain(host)
        self._refusals[bucket] = self._refusals.get(bucket, 0) + 1
        self._successes[bucket] = 0
        if self._refusals[bucket] < _REFUSALS_BEFORE_BACKOFF:
            return

        current = self._backoff.get(bucket, self.rate)
        reduced = max(current / 2.0, _MIN_RATE)
        if reduced < current:
            self._backoff[bucket] = reduced
            log.info(
                "%s refused %d requests in a row; slowing to %.2f req/s",
                bucket, self._refusals[bucket], reduced,
            )

    def note_success(self, host: str) -> None:
        """Clear the refusal streak, and climb back toward the base rate.

        Without recovery a single rough patch throttles the rest of the crawl.
        Doubling back after a sustained run of successes returns a host that has
        stopped refusing us to full speed.
        """
        bucket = registrable_domain(host)
        self._refusals[bucket] = 0
        if bucket not in self._backoff:
            return

        self._successes[bucket] = self._successes.get(bucket, 0) + 1
        if self._successes[bucket] < _SUCCESSES_BEFORE_RECOVERY:
            return

        self._successes[bucket] = 0
        restored = min(self._backoff[bucket] * 2.0, self.rate)
        if restored >= self.rate:
            del self._backoff[bucket]
            log.info("%s is answering again; back to %.2f req/s", bucket, self.rate)
        else:
            self._backoff[bucket] = restored
            log.info("%s is answering again; raising to %.2f req/s", bucket, restored)

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
