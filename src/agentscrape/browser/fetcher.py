"""Cheap HTTP fetching — the default path. The browser is the escalation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from ..config import settings
from ..urls import content_hash, host_of
from .ratelimit import get_rate_limiter

log = logging.getLogger("agentscrape.fetch")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    text: str
    content_type: str
    ok: bool
    error: str | None = None
    elapsed_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type or (
            not self.content_type and self.text.lstrip()[:1] == "<"
        )

    @property
    def content_hash(self) -> str:
        return content_hash(self.text)


class Fetcher:
    """Shared async HTTP client with per-domain rate limiting and bounded retries."""

    def __init__(self, *, concurrency: int | None = None) -> None:
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(
            concurrency or settings.in_site_fetch_concurrency
        )
        self._limiter = get_rate_limiter()

    async def __aenter__(self) -> Fetcher:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.fetch_timeout_seconds),
            follow_redirects=True,
            max_redirects=5,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None

    async def get(
        self, url: str, *, attempts: int = 3, timeout: float | None = None
    ) -> FetchResult:
        """GET with rate limiting and exponential backoff on transient failures."""
        if self._client is None:
            raise RuntimeError("Fetcher used outside its async context manager")

        host = host_of(url)
        last_error: str | None = None
        last_status: int | None = None

        for attempt in range(attempts):
            async with self._semaphore:
                await self._limiter.acquire(host)
                start = asyncio.get_running_loop().time()
                try:
                    response = await self._client.get(
                        url, timeout=timeout or httpx.USE_CLIENT_DEFAULT
                    )
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log.debug("fetch error %s (attempt %d): %s", url, attempt + 1, exc)
                else:
                    elapsed = int((asyncio.get_running_loop().time() - start) * 1000)
                    last_status = response.status_code
                    if response.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                        last_error = f"HTTP {response.status_code}"
                        await self._backoff(attempt, response)
                        continue
                    body = ""
                    if response.status_code < 400:
                        raw = response.content[: settings.max_html_bytes]
                        body = raw.decode(response.encoding or "utf-8", errors="replace")
                    return FetchResult(
                        url=url,
                        final_url=str(response.url),
                        status=response.status_code,
                        text=body,
                        content_type=response.headers.get("content-type", "").lower(),
                        ok=response.status_code < 400,
                        error=None if response.status_code < 400 else f"HTTP {response.status_code}",
                        elapsed_ms=elapsed,
                        headers=dict(response.headers),
                    )
            if attempt < attempts - 1:
                await self._backoff(attempt, None)

        return FetchResult(
            url=url, final_url=url, status=last_status, text="", content_type="",
            ok=False, error=last_error or "request failed",
        )

    async def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        """Honour Retry-After when the server sends one; otherwise 0.5s, 1s, 2s."""
        delay = 0.5 * (2**attempt)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = min(float(retry_after), 30.0)
        await asyncio.sleep(delay)

    async def get_many(
        self, urls: list[str], *, attempts: int = 3
    ) -> list[FetchResult]:
        """Concurrent cheap fetches. Parallelism here is safe; browser work is not.

        Pass attempts=1 for speculative probes. Certificate-transparency logs are
        full of hostnames that no longer resolve, and retrying each of those three
        times with backoff turns discovery into minutes of waiting on dead hosts.
        """
        return list(await asyncio.gather(*(self.get(u, attempts=attempts) for u in urls)))
