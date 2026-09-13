"""Search-engine discovery behind a provider interface.

Deliberately a stub: with no API key configured the provider is `none` and the
step is skipped silently, so the system has no hard dependency on a paid hosted
service. Wiring a real provider is a config change plus one subclass.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from ..config import settings

log = logging.getLogger("agentscrape.discovery.search")

# Queries that tend to surface roster pages on an institutional domain.
DEFAULT_QUERIES = (
    "residents", "fellows", "house staff directory",
    "current residents", "residency program people",
)


class SearchProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    async def search(self, query: str, *, site: str, limit: int = 20) -> list[str]:
        """Return result URLs for `query` restricted to `site`."""

    @property
    def enabled(self) -> bool:
        return True


class NullSearchProvider(SearchProvider):
    """Used whenever no API key is configured. Never errors, returns nothing."""

    name = "none"

    async def search(self, query: str, *, site: str, limit: int = 20) -> list[str]:
        return []

    @property
    def enabled(self) -> bool:
        return False


class BraveSearchProvider(SearchProvider):
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def search(self, query: str, *, site: str, limit: int = 20) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    self.endpoint,
                    params={"q": f"site:{site} {query}", "count": min(limit, 20)},
                    headers={
                        "X-Subscription-Token": self.api_key,
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            log.warning("brave search failed (%s); skipping this source", exc)
            return []
        return [
            item["url"]
            for item in payload.get("web", {}).get("results", [])
            if item.get("url")
        ]


class SerperSearchProvider(SearchProvider):
    name = "serper"
    endpoint = "https://google.serper.dev/search"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def search(self, query: str, *, site: str, limit: int = 20) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    self.endpoint,
                    json={"q": f"site:{site} {query}", "num": min(limit, 20)},
                    headers={"X-API-KEY": self.api_key},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            log.warning("serper search failed (%s); skipping this source", exc)
            return []
        return [item["link"] for item in payload.get("organic", []) if item.get("link")]


def get_search_provider() -> SearchProvider:
    """Resolve the configured provider. Falls back to the no-op when unusable."""
    provider = (settings.search_provider or "none").strip().lower()
    key = (settings.search_api_key or "").strip()
    if provider in ("", "none") or not key:
        if provider not in ("", "none") and not key:
            log.info("search provider %r configured but no API key; skipping search", provider)
        return NullSearchProvider()
    if provider == "brave":
        return BraveSearchProvider(key)
    if provider == "serper":
        return SerperSearchProvider(key)
    log.warning("unknown search provider %r; skipping search", provider)
    return NullSearchProvider()


async def discover_via_search(site_host: str, *, limit_per_query: int = 15) -> list[str]:
    provider = get_search_provider()
    if not provider.enabled:
        return []
    found: dict[str, None] = {}
    for query in DEFAULT_QUERIES:
        for url in await provider.search(query, site=site_host, limit=limit_per_query):
            found.setdefault(url, None)
    log.info("search (%s): %d urls for %s", provider.name, len(found), site_host)
    return list(found)
