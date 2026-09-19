"""URL canonicalization and hashing.

Canonicalization is what makes the per-SiteRun visited set actually prevent
re-fetches: without it `/residents`, `/residents/`, `/residents?utm_source=x` and
`/residents#top` are four different pages.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking parameters that never change what a page returns.
_JUNK_PARAMS = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|msclkid|mc_[a-z]+|_ga|_gl|ref|referrer|source|"
    r"campaign|hsa_[a-z]+|igshid|si)$",
    re.IGNORECASE,
)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_INDEX_SUFFIX = re.compile(r"/(index|default|home)\.(html?|php|aspx?|jsp)$", re.IGNORECASE)


def canonicalize(url: str, *, base: str | None = None) -> str | None:
    """Normalize a URL for comparison. Returns None for anything unfetchable."""
    from urllib.parse import urljoin

    if not url:
        return None
    url = url.strip()
    if not url or url.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
        return None
    # A page can link anything, and both `urljoin` and `urlsplit` raise on a
    # malformed authority — "Invalid IPv6 URL" for a stray bracket, "Port out of
    # range" for a typo. Unguarded, one bad href on one page aborted the whole
    # site run: an Arizona crawl lost 650 steps of remaining budget to a single
    # link. Both calls are inside the guard because `urljoin` parses too, and
    # guarding only the second one left the crash exactly where it was. An
    # unparseable URL is simply not fetchable, which is what None already means.
    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None

    netloc = host
    if port and port != _DEFAULT_PORTS.get(parts.scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    path = _INDEX_SUFFIX.sub("/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = urlencode(
        sorted(
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _JUNK_PARAMS.match(k)
        )
    )
    return urlunsplit((parts.scheme, netloc, path, query, ""))  # fragment dropped


def url_hash(url: str) -> str:
    """Stable 64-char key for the visited set and known-path uniqueness."""
    return hashlib.sha256((canonicalize(url) or url).encode()).hexdigest()


def content_hash(content: str | bytes) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8", "replace")
    return hashlib.sha256(content).hexdigest()


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a public-suffix dependency.

    Handles the multi-part suffixes that actually show up in this dataset
    (.ac.uk, .edu.au, .k12.*.us). Used for grouping and rate-limit buckets, never
    for a security decision.
    """
    host = host.lower().strip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    two_part_suffixes = {
        "ac.uk", "co.uk", "org.uk", "gov.uk", "nhs.uk", "edu.au", "gov.au",
        "com.au", "org.au", "edu.cn", "ac.jp", "edu.sg", "edu.hk", "ac.nz",
        "edu.in", "ac.in", "edu.br", "edu.mx", "co.za", "ac.za", "edu.tr",
    }
    if ".".join(labels[-2:]) in two_part_suffixes:
        return ".".join(labels[-3:])
    if len(labels) >= 4 and labels[-1] == "us" and labels[-3] == "k12":
        return ".".join(labels[-4:])
    return ".".join(labels[-2:])


def same_registrable_domain(a: str, b: str) -> bool:
    return registrable_domain(a) == registrable_domain(b)


def in_scope(host: str, allowed_domains: set[str] | frozenset[str]) -> bool:
    """True when `host` belongs to one of the institution's own domains.

    An academic medical centre routinely spans two registrable domains: the
    university and the health system it staffs. Chicago's trainees are published
    on uchicagomedicine.org while its GME site is uchicago.edu, and Arizona's
    residents carry bannerhealth.com addresses. Judging scope by the entry
    domain alone puts the rosters permanently out of reach.
    """
    if not host:
        return False
    return registrable_domain(host) in allowed_domains


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def entry_url(url: str) -> str:
    """A crawl entry point as the site serves it: a bare host gets its "/",
    a page keeps its own path. Appending a slash to every entry turned
    medicine.arizona.edu/education/residency-fellowship into a 404."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return url
    return url if path else f"{url}/"


def home_url(url: str) -> str:
    """The home page of the site `url` is on, keeping its scheme and port."""
    parts = urlsplit(url)
    return f"{parts.scheme or 'https'}://{parts.netloc}/"


def path_depth(url: str) -> int:
    try:
        path = urlsplit(url).path
    except ValueError:
        return 0
    return len([s for s in path.split("/") if s])
