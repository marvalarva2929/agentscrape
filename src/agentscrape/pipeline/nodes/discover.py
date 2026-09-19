"""Stage 2 and 3: build the candidate list, known-good paths first.

No browser is involved. This is the compute-reduction step: arriving with a good
ordered target list instead of exploring blind is the biggest efficiency lever
in the system.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

from ...config import settings
from ...db.repositories.sites import active_known_paths
from ...discovery.crt_sh import discover_subdomains, subdomain_seed_urls
from ...discovery.scoring import score_url
from ...discovery.search import discover_via_search
from ...discovery.sitemap import (
    LinkContext,
    discover_from_sitemaps,
    extract_link_contexts,
    fetch_robots,
)
from ...llm.triage import LinkDecision, heuristic_priority, triage_hosts, triage_links
from ...urls import canonicalize, home_url, host_of, in_scope, registrable_domain
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.discover")

# Probing every CT-log subdomain would be slower than the crawl it saves.
MAX_SUBDOMAIN_PROBES = 300
SUBDOMAIN_SITEMAP_CAP = 4
SUBDOMAIN_URL_CAP = 3_000
# Hosts the site itself pointed at, so each one is real and worth a sitemap.
# A large medical school links 20-40 departmental hosts.
MAX_HOST_SITEMAPS = 200
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

    await deps.note(state, f"Reading {root_domain}'s sitemaps and home page")
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
    if home is None or not home.ok or not home.is_html:
        # The school sheet's entry page can be dead (Arizona's and UVA's hub
        # links both returned 404): fall back to the host's home page, and hand
        # the working page to the planner as the site's entry.
        fallback = home_url(root_url)
        if fallback != root_url:
            retry = await _bounded(
                "homepage fallback", deps.fetcher.get(fallback, attempts=2), None,
                timeout=max(remaining(), 15.0),
            )
            if retry is not None and retry.ok and retry.is_html:
                log.warning(
                    "entry page %s failed (%s); starting from %s instead",
                    root_url, home.error or home.status if home else "timeout", fallback,
                )
                await deps.note(state, f"The entry page {root_url} is unavailable; starting from {fallback}")
                home, root_url = retry, fallback
                state = {**state, "root_url": fallback}
    link_text: dict[str, LinkContext] = {}
    if home is not None and home.ok and home.is_html:
        discovered.setdefault(home.final_url, None)
        for link in extract_link_contexts(home.text, home.final_url, allowed_domains=allowed):
            discovered.setdefault(link.url, None)
            link_text.setdefault(link.url, link)

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
    # It surfaces a department that nothing links to - Chicago publishes its
    # internal medicine residents on imr.bsd.uchicago.edu, which no page in the
    # GME hub mentions.
    ct_hosts = (
        []
        if out_of_time()
        else await _bounded(
            "crt.sh", discover_subdomains(root_domain), [], timeout=remaining()
        )
    )

    # Which hosts to explore is the model's call, not a hostname deny-list: a
    # list that contained "emergency" and "global" was ruling out emergency
    # medicine and global health departments. Hosts are shown with sample paths
    # so the model can see what each one actually serves.
    observed = _observed_host_counts(discovered, root_domain, allowed)
    ct_candidates = [
        h for h in dict.fromkeys(ct_hosts)
        if h not in observed and h != root_domain and in_scope(h, allowed)
        and not h.startswith("*")
    ]
    samples: dict[str, list[str]] = {}
    for url in discovered:
        host = host_of(url)
        if host in observed and len(samples.setdefault(host, [])) < 5:
            path = urlsplit(url).path or "/"
            if path not in samples[host]:
                samples[host].append(path)
    for host in ct_candidates:
        samples.setdefault(host, [])
    if samples:
        await deps.note(
            state, f"Found {len(samples)} related websites; the agent is deciding which to explore"
        )
    skipped_hosts = await triage_hosts(samples, root_domain=root_domain, meter=deps.meter)

    ct_keep = [h for h in ct_candidates if h not in skipped_hosts][:MAX_SUBDOMAIN_PROBES]
    if ct_keep and not out_of_time():
        # Most certificate-log hostnames no longer exist. A DNS lookup says so
        # in milliseconds and is not an HTTP request against the institution,
        # so only hosts that resolve spend the domain's shared request budget.
        ct_keep = await _resolving(ct_keep, timeout=min(remaining(), 15.0))
    if ct_keep and not out_of_time():
        # attempts=1: most CT-log hostnames no longer resolve, and retrying each
        # dead host with backoff dominates the whole discovery stage. Each host
        # is its own task and whatever answered by the deadline is kept: one
        # all-or-nothing wait lost all 300 probes when the budget ran out, and
        # with them UChicago's internal medicine residents on imr.bsd.
        tasks = [
            asyncio.ensure_future(deps.fetcher.get(url, attempts=1))
            for url in subdomain_seed_urls(ct_keep)
        ]
        done, late = await asyncio.wait(tasks, timeout=max(remaining(), 1.0))
        for task in late:
            task.cancel()
        if late:
            log.info("crt.sh: %d of %d probes still waiting at the deadline", len(late), len(tasks))
        probes = [t.result() for t in done if not t.cancelled() and t.exception() is None]
        alive = 0
        for result in probes:
            if not result.ok or not result.is_html:
                continue
            alive += 1
            discovered.setdefault(result.final_url, None)
            for link in extract_link_contexts(
                result.text, result.final_url, allowed_domains=allowed
            ):
                discovered.setdefault(link.url, None)
                link_text.setdefault(link.url, link)
        log.info(
            "crt.sh: %d of %d probed hostnames answered for %s",
            alive, len(ct_keep), root_domain,
        )

    # Every kept host now in hand - linked, affiliated or certificate-logged -
    # gets its own sitemap walk. Departmental sites are usually siblings of the
    # entry point rather than children: surgery.<univ>.edu and obgyn.<univ>.edu
    # sit beside medicine.<univ>.edu, each with its own sitemap and rosters.
    # Walked concurrently: rate limiting is per host, so they do not contend.
    counts = _observed_host_counts(discovered, root_domain, allowed)
    hosts_to_walk = sorted(
        (h for h in counts if h not in skipped_hosts),
        key=lambda h: -counts[h],
    )[:MAX_HOST_SITEMAPS]
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
        await deps.note(
            state,
            f"Exploring {len(hosts_to_walk)} websites the agent kept "
            f"(ruled out {len(skipped_hosts)} as unrelated)",
        )
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
    known_urls = {canonicalize(p.url) or p.url for p in known}
    for url in known_urls:
        discovered.setdefault(url, None)

    # The model triages everything found, in batches. Nothing is dropped for
    # lacking a keyword; only what the model calls obviously useless, pages on
    # hosts it ruled out, and non-HTML files.
    from ..nodes.extract import link_key, merge_frontier

    to_triage: list[dict] = []
    triaged: set[str] = set()
    for url in discovered:
        canonical = canonicalize(url)
        if not canonical or host_of(canonical) in skipped_hosts:
            continue
        key = link_key(canonical)
        if key in triaged:
            continue
        triaged.add(key)
        context = link_text.get(url)
        to_triage.append(context.as_dict() if context else {"url": canonical})
    if state.get("crawl_strategy") == "hybrid":
        # The HTML pass that follows ranks these by what the pages hold, so
        # the keyword heuristic is enough to decide what it fetches first.
        decisions = []
        for link in to_triage:
            score = score_url(link["url"], title=link.get("text") or None).score
            decisions.append(LinkDecision(link["url"], heuristic_priority(score), None, False, score))
    else:
        await deps.note(
            state, f"Found {len(to_triage):,} pages; the agent is ranking which to read first"
        )
        decisions = await triage_links(
            to_triage, source=f"sitemaps and home pages of {root_domain}", meter=deps.meter,
        )
    additions = [
        {
            "url": d.url, "score": d.heuristic,
            # Proven yield is a strong hint, but no longer a +100 pin that
            # freezes the ranking to whatever the last run happened to find.
            "priority": max(d.priority, 90.0) if d.url in known_urls else d.priority,
            "program": d.program,
            "is_known_path": d.url in known_urls,
        }
        for d in decisions
        if not d.skipped and d.heuristic > -100
    ]
    candidates = merge_frontier([], 0, additions)

    log.info(
        "discovery for %s: %d urls found, %d candidates kept (%d known paths, %d hosts skipped)",
        root_domain, len(discovered), len(candidates), len(known_urls), len(skipped_hosts),
    )

    if state.get("crawl_strategy") == "hybrid":
        await deps.note(
            state,
            f"Found {len(discovered):,} pages across "
            f"{len(_observed_host_counts(discovered, root_domain, allowed)) + 1} sites",
        )
        return {
            **state,
            "candidates": candidates,
            "candidates_considered": len(discovered),
            "triaged": sorted(triaged),
            "cursor": 0,
        }
    await deps.note(
        state,
        f"Mapped {len(discovered):,} pages across {len(_observed_host_counts(discovered, root_domain, allowed)) + 1} "
        f"sites; the agent kept {len(candidates):,} worth reading",
    )
    return {
        **state,
        "candidates": candidates,
        "candidates_considered": len(discovered),
        "triaged": sorted(triaged),
        "cursor": 0,
    }


async def _resolving(hosts: list[str], *, timeout: float) -> list[str]:
    """The hosts with a DNS record, in their original order."""
    loop = asyncio.get_running_loop()

    async def resolves(host: str) -> bool:
        try:
            await asyncio.wait_for(loop.getaddrinfo(host, 443), timeout=5.0)
            return True
        except Exception:
            return False

    tasks = [asyncio.ensure_future(resolves(h)) for h in hosts]
    done, late = await asyncio.wait(tasks, timeout=max(timeout, 1.0))
    for task in late:
        task.cancel()
    alive = [
        host for host, task in zip(hosts, tasks, strict=True)
        if task in done and not task.cancelled() and task.result()
    ]
    log.info("crt.sh: %d of %d candidate hostnames resolve", len(alive), len(hosts))
    return alive


def _observed_host_counts(
    urls: dict[str, None], root_domain: str, allowed: set[str]
) -> dict[str, int]:
    """In-scope hosts the site itself pointed at, with how many URLs each holds."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for url in urls:
        host = host_of(url)
        if host and host != root_domain and in_scope(host, allowed):
            counts[host] += 1
    return dict(counts)


def host_for(url: str) -> str:
    return host_of(url)
