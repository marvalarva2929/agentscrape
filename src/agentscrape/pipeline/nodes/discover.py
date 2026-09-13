"""Stage 2 and 3: build the candidate list, known-good paths first.

No browser is involved. This is the compute-reduction step: arriving with a good
ordered target list instead of exploring blind is the biggest efficiency lever
in the system.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ...config import settings
from ...db.repositories.sites import active_known_paths
from ...discovery.crt_sh import discover_subdomains, subdomain_seed_urls
from ...discovery.scoring import rank_candidates
from ...discovery.search import discover_via_search
from ...discovery.sitemap import discover_from_sitemaps, extract_links, fetch_robots
from ...urls import canonicalize, host_of
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.discover")

# Probing every CT-log subdomain would be slower than the crawl it saves.
MAX_SUBDOMAIN_PROBES = 12
SUBDOMAIN_SITEMAP_CAP = 4
SUBDOMAIN_URL_CAP = 3_000



async def _bounded(label: str, coro, default, *, timeout: float):
    """Run one discovery source under a timeout.

    Discovery is best-effort by nature: sitemaps 404, crt.sh rate-limits,
    subdomains hang. A slow source must degrade to "found nothing" rather than
    hold the site's slot, so every source is individually bounded.

    The timeout passed in is the *remaining* stage budget, not a fixed per-source
    value. Checking the budget only before starting a source is not enough: one
    begun just under the deadline could still run for its full timeout past it,
    which is how a 180s budget turned into six minutes.
    """
    if timeout <= 0:
        log.info("discovery budget spent; skipping source %s", label)
        # The coroutine was already constructed by the caller, so close it
        # explicitly rather than leaking a "never awaited" warning.
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        return default
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        log.warning("discovery source %s timed out; continuing without it", label)
        return default
    except Exception as exc:
        log.warning("discovery source %s failed (%s); continuing without it", label, exc)
        return default


async def discover_links(state: SiteState, deps: PipelineDeps) -> SiteState:
    root_url = state["root_url"]
    root_domain = state["root_domain"]
    discovered: dict[str, None] = {}
    deadline = time.monotonic() + settings.discovery_timeout_seconds

    def remaining() -> float:
        """Seconds of stage budget left, capped by the per-source timeout."""
        return min(
            settings.discovery_source_timeout_seconds, deadline - time.monotonic()
        )

    def out_of_time() -> bool:
        if time.monotonic() >= deadline:
            log.warning(
                "discovery budget of %ss reached for %s; ranking what we have",
                settings.discovery_timeout_seconds, root_domain,
            )
            return True
        return False

    robots = await _bounded("robots", fetch_robots(deps.fetcher, root_url), None, timeout=remaining())
    if robots is None:
        from ...discovery.sitemap import RobotsInfo

        robots = RobotsInfo()
    if robots.crawl_delay:
        # Honoured even though Disallow is not enforced: a site that asks for a
        # slower rate gets one.
        from ...browser.ratelimit import get_rate_limiter

        get_rate_limiter().note_crawl_delay(root_domain, robots.crawl_delay)

    for url in await _bounded(
        "sitemaps", discover_from_sitemaps(deps.fetcher, root_url, robots), [], timeout=remaining()):
        discovered.setdefault(url, None)

    # Certificate transparency surfaces departmental subdomains that are never
    # linked from the homepage. Degrades to nothing if crt.sh is unavailable.
    hosts = (
        []
        if out_of_time()
        else await _bounded("crt.sh", discover_subdomains(root_domain), [], timeout=remaining())
    )
    interesting = _rank_subdomains(hosts, root_domain)[:MAX_SUBDOMAIN_PROBES]
    if interesting and not out_of_time():
        seeds = subdomain_seed_urls(interesting)
        # attempts=1: most CT-log hostnames no longer resolve, and retrying each
        # dead host with backoff dominates the whole discovery stage.
        probes = await _bounded(
            "subdomain-probe", deps.fetcher.get_many(seeds, attempts=1), [], timeout=remaining())
        for result in probes:
            if not result.ok or not result.is_html:
                continue
            discovered.setdefault(result.final_url, None)
            for link in extract_links(result.text, result.final_url):
                discovered.setdefault(link, None)
        # Departmental subdomains get a shallower sitemap walk than the root:
        # this is the dominant cost of discovery and the root sitemap already
        # covers the bulk of the institution.
        for host in interesting:
            if out_of_time():
                break
            sub_robots = await _bounded(
                f"robots:{host}", fetch_robots(deps.fetcher, f"https://{host}/"), None, timeout=remaining())
            if sub_robots is None:
                continue
            for url in await _bounded(
                f"sitemap:{host}",
                discover_from_sitemaps(
                    deps.fetcher, f"https://{host}/", sub_robots,
                    max_sitemaps=SUBDOMAIN_SITEMAP_CAP, max_urls=SUBDOMAIN_URL_CAP,
                ),
                [],
                timeout=remaining(),
            ):
                discovered.setdefault(url, None)

    # Optional; a no-op unless a search API key is configured.
    for url in await _bounded("search", discover_via_search(root_domain), [], timeout=remaining()):
        canonical = canonicalize(url)
        if canonical:
            discovered.setdefault(canonical, None)

    # Always include the homepage's own links: some sites publish no sitemap.
    home = await _bounded(
        "homepage", deps.fetcher.get(root_url, attempts=2), None,
        timeout=max(remaining(), 15.0),
    )
    if home is not None and home.ok and home.is_html:
        discovered.setdefault(home.final_url, None)
        for link in extract_links(home.text, home.final_url):
            discovered.setdefault(link, None)

    async with deps.sessionmaker() as session:
        known = await active_known_paths(session, state["site_id"])
    known_scores = {canonicalize(p.url) or p.url: p.score for p in known}
    for url in known_scores:
        discovered.setdefault(url, None)

    ranked = rank_candidates(
        list(discovered),
        known_paths=known_scores,
        limit=settings.max_candidates,
        min_score=1.0,  # ignore pages with no positive roster signal at all
    )

    log.info(
        "discovery for %s: %d urls found, %d candidates kept (%d known paths)",
        root_domain, len(discovered), len(ranked), len(known_scores),
    )

    return {
        **state,
        "candidates": [
            {"url": c.url, "score": c.score, "is_known_path": c.is_known_path}
            for c in ranked
        ],
        "candidates_considered": len(discovered),
        "cursor": 0,
    }


def _rank_subdomains(hosts: list[str], root_domain: str) -> list[str]:
    """Prefer subdomains that look like clinical departments over infrastructure."""
    from ...domain.specialty import normalize_specialty

    noise = (
        "mail", "smtp", "imap", "vpn", "webmail", "autodiscover", "ns1", "ns2",
        "mx", "cdn", "static", "assets", "img", "test", "dev", "staging", "old",
        "legacy", "api", "login", "sso", "auth", "proxy", "gateway",
    )
    scored: list[tuple[float, str]] = []
    for host in hosts:
        if host == root_domain:
            continue
        label = host.split(".")[0].lower()
        if label in noise or any(label.startswith(f"{n}-") for n in noise):
            continue
        score = 0.0
        if normalize_specialty(label).canonical:
            score += 10.0
        if any(k in label for k in ("med", "surg", "resident", "gme", "edu", "clinic")):
            score += 3.0
        if len(host.split(".")) == len(root_domain.split(".")) + 1:
            score += 1.0  # a direct child, not a deep nesting
        if score > 0:
            scored.append((score, host))
    scored.sort(reverse=True)
    return [host for _, host in scored]


def host_for(url: str) -> str:
    return host_of(url)
