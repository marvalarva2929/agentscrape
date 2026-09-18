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
from ...urls import canonicalize, host_of, in_scope, registrable_domain
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.discover")

# Probing every CT-log subdomain would be slower than the crawl it saves.
MAX_SUBDOMAIN_PROBES = 80
SUBDOMAIN_SITEMAP_CAP = 4
SUBDOMAIN_URL_CAP = 3_000
# Hosts the site itself pointed at, so each one is real and worth a sitemap.
# A large medical school links 20-40 departmental hosts.
MAX_HOST_SITEMAPS = 70
# How many of those to walk at once. Each walk is a robots fetch plus a few
# sitemap fetches against its own host, so this is bounded by courtesy to the
# institution's server rather than by anything local.
HOST_WALK_CONCURRENCY = 6


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
    allowed = set(state.get("allowed_domains") or [registrable_domain(root_domain)])
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

    # Always include the homepage's own links: some sites publish no sitemap, and
    # the hosts it links to are the strongest evidence of which sibling
    # departments exist. Done before the per-host walk below so those hosts are
    # already in hand.
    home = await _bounded(
        "homepage", deps.fetcher.get(root_url, attempts=2), None,
        timeout=max(remaining(), 15.0),
    )
    if home is not None and home.ok and home.is_html:
        discovered.setdefault(home.final_url, None)
        for link in extract_links(home.text, home.final_url, allowed_domains=allowed):
            discovered.setdefault(link, None)

    # Departmental sites are usually *siblings* of the entry point, not children
    # of it: surgery.<univ>.edu and obgyn.<univ>.edu sit beside
    # medicine.<univ>.edu, each with its own sitemap and its own rosters. Walking
    # only the entry host's sitemap reached a handful of their pages by way of
    # cross-links and none of their roster pages at all, so every host already
    # present in the candidate set gets its own sitemap walk.
    #
    # The hosts are walked concurrently. Rate limiting is per domain, so
    # separate hosts do not contend, and forty sequential robots-plus-sitemap
    # walks spent the entire stage budget before the last department was
    # reached.
    # An affiliated domain has to be seeded, not merely permitted: the entry
    # host may link to its front page once or not at all, and Chicago's trainee
    # rosters live entirely on the health system's domain.
    for domain in sorted(allowed):
        if domain != registrable_domain(root_domain):
            discovered.setdefault(f"https://www.{domain}/", None)

    # Certificate transparency, before the sitemap walk rather than after it.
    # It surfaces a department that nothing links to — Chicago publishes its
    # internal medicine residents on imr.bsd.uchicago.edu, which no page in the
    # GME hub mentions. Running it after the walk meant those hosts contributed
    # only their homepage links and never had their sitemaps read at all, which
    # is where the rosters are.
    ct_hosts = (
        []
        if out_of_time()
        else await _bounded(
            "crt.sh", discover_subdomains(root_domain), [], timeout=remaining()
        )
    )
    interesting = _rank_subdomains(ct_hosts, root_domain)[:MAX_SUBDOMAIN_PROBES]
    if interesting and not out_of_time():
        # attempts=1: most CT-log hostnames no longer resolve, and retrying each
        # dead host with backoff dominates the whole discovery stage.
        probes = await _bounded(
            "subdomain-probe",
            deps.fetcher.get_many(subdomain_seed_urls(interesting), attempts=1),
            [],
            timeout=remaining(),
        )
        alive = 0
        for result in probes:
            if not result.ok or not result.is_html:
                continue
            alive += 1
            discovered.setdefault(result.final_url, None)
            for link in extract_links(
                result.text, result.final_url, allowed_domains=allowed
            ):
                discovered.setdefault(link, None)
        log.info(
            "crt.sh: %d of %d probed hostnames answered for %s",
            alive, len(interesting), root_domain,
        )

    # Every in-scope host now in hand — linked, affiliated or certificate-logged
    # — gets its own sitemap walk. Departmental sites are usually *siblings* of
    # the entry point rather than children: surgery.<univ>.edu and
    # obgyn.<univ>.edu sit beside medicine.<univ>.edu, each with its own sitemap
    # and its own rosters.
    #
    # The hosts are walked concurrently. Rate limiting is per domain, so
    # separate hosts do not contend, and sixty sequential robots-plus-sitemap
    # walks spent the entire stage budget before the last department was reached.
    hosts_to_walk = _observed_hosts(discovered, root_domain, allowed)[:MAX_HOST_SITEMAPS]
    gate = asyncio.Semaphore(HOST_WALK_CONCURRENCY)

    async def walk(host: str) -> list[str]:
        async with gate:
            if out_of_time():
                return []
            host_root = f"https://{host}/"
            host_robots = await _bounded(
                f"robots:{host}", fetch_robots(deps.fetcher, host_root), None,
                timeout=remaining(),
            )
            if host_robots is None:
                return []
            return await _bounded(
                f"sitemap:{host}",
                discover_from_sitemaps(
                    deps.fetcher, host_root, host_robots,
                    max_sitemaps=SUBDOMAIN_SITEMAP_CAP, max_urls=SUBDOMAIN_URL_CAP,
                ),
                [],
                timeout=remaining(),
            )

    if hosts_to_walk:
        for found in await asyncio.gather(*(walk(h) for h in hosts_to_walk)):
            for url in found:
                discovered.setdefault(url, None)
        log.info(
            "walked sitemaps for %d in-scope hosts beside %s",
            len(hosts_to_walk), root_domain,
        )

    # Optional; a no-op unless a search API key is configured.
    for url in await _bounded("search", discover_via_search(root_domain), [], timeout=remaining()):
        canonical = canonicalize(url)
        if canonical:
            discovered.setdefault(canonical, None)

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


# Hostnames that serve assets or plumbing rather than pages. Sitemap-walking one
# costs a round trip per probe and returns nothing, and a CDN host attracts
# enough links to sort near the top of the observed-host list without this.
_INFRASTRUCTURE_LABELS = frozenset({
    "www", "web", "mail", "smtp", "imap", "vpn", "webmail", "autodiscover",
    "ns1", "ns2", "mx", "cdn", "static", "assets", "asset", "img", "images",
    "media", "files", "download", "downloads", "test", "dev", "staging",
    "stage", "old", "legacy", "api", "login", "sso", "auth", "proxy",
    "gateway", "fonts", "cache", "s3", "storage", "upload", "uploads",
    "analytics", "tracking", "status", "monitor", "mirror", "links", "go",
    "redirect",
})
# A medical school shares its registrable domain with the whole university, so
# every sibling host is in scope by that test alone. These are the ones no
# teaching hospital publishes a roster on. A deny-list is the right shape here:
# university administration uses a small vocabulary that repeats across
# institutions, whereas the clinical side does not — a departmental host can be
# called anything from "obgyn" to "arthritis" to "heart", and an allow-list
# would have to anticipate all of them.
_NON_CLINICAL_LABELS = frozenset({
    "shop", "store", "bookstore", "map", "maps", "parking", "transit",
    "housing", "dining", "athletics", "sports", "tickets", "giving",
    "donate", "foundation", "advancement", "alumniassociation", "library",
    "libraries", "press", "news", "newsroom", "magazine", "corporate",
    "talent", "careers", "jobs", "hr", "payroll", "benefits", "police",
    "safety", "emergency", "it", "its", "help", "helpdesk", "support",
    "admissions", "apply", "registrar", "financialaid", "bursar", "catalog",
    "calendar", "events", "international", "global", "honors", "law",
    "business", "engineering", "agriculture", "arts", "music", "theatre",
    "environment", "geo", "optics", "astronomy", "physics", "math",
})


# "www." in front of a real department name says nothing about the department.
_WWW_LABELS = frozenset({"www", "web"})


def _leading_label(host: str) -> str:
    """The label that names the host, ignoring a bare `www.` in front of it."""
    labels = [label.lower() for label in host.split(".")]
    if labels and labels[0] in _WWW_LABELS and len(labels) > 3:
        # `www.surgery.<univ>.edu` is the surgery department; `www.<univ>.edu`
        # is the university's front page and has no label of its own.
        return labels[1]
    return labels[0] if labels else ""


def _is_affiliate_apex(host: str, root_domain: str) -> bool:
    """True for the front page of an *affiliated* domain, with or without `www.`.

    `www.uchicagomedicine.org` is the health system's main site, not a `www`
    infrastructure host, and judging it by its first label ruled the whole
    affiliated domain out of scope — with it, every Chicago roster.

    The entry point's own apex is deliberately excluded: `www.arizona.edu` is
    the university's front page, not the medical school's, and walking it costs
    a great deal for nothing.
    """
    if registrable_domain(host) == registrable_domain(root_domain):
        return False
    return host.removeprefix("www.") == registrable_domain(host)


def _is_infrastructure(host: str) -> bool:
    label = _leading_label(host)
    return label in _INFRASTRUCTURE_LABELS or any(
        label.startswith(f"{n}-") for n in _INFRASTRUCTURE_LABELS
    )


def _is_out_of_scope(host: str, root_domain: str) -> bool:
    """True for a sibling host that belongs to the university, not the hospital.

    Only the host's own label is judged, and never for the entry host itself: a
    run rooted at `law.<univ>.edu` would be a different job, not this one.
    """
    if host == root_domain or _is_affiliate_apex(host, root_domain):
        return False
    label = _leading_label(host)
    return (
        label in _NON_CLINICAL_LABELS
        or label in _WWW_LABELS
        or _is_infrastructure(host)
    )


def _observed_hosts(
    urls: dict[str, None], root_domain: str, allowed: set[str] | None = None
) -> list[str]:
    """Hosts inside the institution that the site itself pointed at.

    Ranked by how many of its pages we already hold and by whether its label
    names a clinical department, so a real department outranks a one-link
    mention of the library. Far better evidence than certificate transparency:
    these hosts demonstrably exist and demonstrably belong to the institution.
    """
    from collections import Counter

    scope = allowed or {registrable_domain(root_domain)}
    counts: Counter[str] = Counter()
    for url in urls:
        host = host_of(url)
        if (
            host
            and host != root_domain
            and in_scope(host, scope)
            and not _is_out_of_scope(host, root_domain)
        ):
            counts[host] += 1

    ranked = _rank_subdomains(list(counts), root_domain)
    interesting = set(ranked)
    # Keep the departmental ordering, then any other in-scope host by link count:
    # a host with 50 of our URLs is a real section of the site whatever it is called.
    rest = sorted(
        (h for h in counts if h not in interesting), key=lambda h: -counts[h]
    )
    return [*ranked, *rest]


def _rank_subdomains(hosts: list[str], root_domain: str) -> list[str]:
    """Prefer subdomains that look like clinical departments over infrastructure."""
    from ...domain.specialty import normalize_specialty

    scored: list[tuple[float, str]] = []
    for host in hosts:
        if _is_out_of_scope(host, root_domain) or host == root_domain:
            continue
        # Same label the scope check judged, so `www.surgery.<univ>.edu` is
        # ranked as the surgery department rather than scoring nothing as "www".
        label = _leading_label(host)
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
