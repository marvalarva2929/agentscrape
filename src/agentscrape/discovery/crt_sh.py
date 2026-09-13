"""Subdomain enumeration via certificate transparency logs (crt.sh).

This is what surfaces departmental subdomains — surgery.hospital.edu,
peds.hospital.edu — that are never linked from the homepage. Free and
unauthenticated. A failure here degrades discovery to sitemap-only rather than
failing the site, so crt.sh being down never blocks a run.
"""

from __future__ import annotations

import json
import logging

import httpx

from ..config import settings
from ..urls import registrable_domain

log = logging.getLogger("agentscrape.discovery.crtsh")

CRT_SH_URL = "https://crt.sh/"


async def discover_subdomains(root_domain: str) -> list[str]:
    """Distinct hostnames under the registrable domain seen in CT logs."""
    if not settings.enable_crt_sh:
        return []

    apex = registrable_domain(root_domain)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.crt_sh_timeout_seconds),
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as client:
            response = await client.get(
                CRT_SH_URL, params={"q": f"%.{apex}", "output": "json"}
            )
            if response.status_code != 200:
                log.warning("crt.sh returned HTTP %s for %s", response.status_code, apex)
                return []
            entries = response.json()
    except (TimeoutError, httpx.HTTPError, json.JSONDecodeError) as exc:
        log.warning("crt.sh lookup failed for %s (%s); continuing without it", apex, exc)
        return []

    hosts: dict[str, None] = {}
    for entry in entries if isinstance(entries, list) else []:
        for name in str(entry.get("name_value", "")).splitlines():
            host = name.strip().lower().lstrip("*.").rstrip(".")
            if not host or " " in host:
                continue
            if host == apex or host.endswith(f".{apex}"):
                hosts.setdefault(host, None)

    log.info("crt.sh: %d distinct hostnames for %s", len(hosts), apex)
    return sorted(hosts)


def subdomain_seed_urls(hosts: list[str], *, scheme: str = "https") -> list[str]:
    """Turn hostnames into root URLs worth probing for a sitemap."""
    return [f"{scheme}://{host}/" for host in hosts]
