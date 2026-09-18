"""Sitemap and robots.txt discovery — the backbone of the candidate list.

robots.txt is read for its `Sitemap:` lines and `Crawl-delay`. Its `Disallow`
rules are parsed but only enforced when RESPECT_ROBOTS is true (see config); the
default is rate-limited crawling without Disallow enforcement.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from html import unescape
from xml.etree import ElementTree

from ..browser.fetcher import Fetcher
from ..config import settings
from ..urls import canonicalize, host_of, in_scope, registrable_domain, same_registrable_domain

log = logging.getLogger("agentscrape.discovery.sitemap")

_MAX_SITEMAPS = 25
_MAX_URLS = 20_000


@dataclass
class RobotsInfo:
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    disallowed: list[str] = field(default_factory=list)

    def is_disallowed(self, path: str) -> bool:
        """Only consulted when settings.respect_robots is true."""
        return any(path.startswith(rule) for rule in self.disallowed if rule)


async def fetch_robots(
    fetcher: Fetcher, root_url: str, *, attempts: int = 2
) -> RobotsInfo:
    from urllib.parse import urljoin

    info = RobotsInfo()
    result = await fetcher.get(urljoin(root_url, "/robots.txt"), attempts=attempts)
    if not result.ok or not result.text:
        return info

    applies = False
    for raw_line in result.text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "sitemap" and value:
            resolved = canonicalize(value, base=root_url)
            if resolved:
                info.sitemaps.append(resolved)
        elif key == "user-agent":
            applies = value == "*" or value.lower() in settings.user_agent.lower()
        elif applies and key == "crawl-delay":
            try:
                info.crawl_delay = float(value)
            except ValueError:
                pass
        elif applies and key == "disallow" and value:
            info.disallowed.append(value)
    return info


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page urls, nested sitemap urls) from one sitemap document."""
    pages: list[str] = []
    nested: list[str] = []
    try:
        root = ElementTree.fromstring(xml_text.strip())
    except ElementTree.ParseError:
        # Some sites serve a plain-text sitemap: one URL per line.
        lines = [
            line.strip()
            for line in xml_text.splitlines()
            if line.strip().startswith("http")
        ]
        return lines[:_MAX_URLS], []

    container = _strip_ns(root.tag)
    for element in root:
        if _strip_ns(element.tag) not in ("url", "sitemap"):
            continue
        location = next(
            (c.text for c in element if _strip_ns(c.tag) == "loc" and c.text), None
        )
        if not location:
            continue
        target = nested if container == "sitemapindex" else pages
        target.append(location.strip())
    return pages[:_MAX_URLS], nested[:_MAX_SITEMAPS]


async def discover_from_sitemaps(
    fetcher: Fetcher, root_url: str, robots: RobotsInfo,
    *, max_sitemaps: int | None = None, max_urls: int | None = None,
) -> list[str]:
    """Walk sitemap.xml and any sitemap index, breadth-first, with hard caps."""
    from urllib.parse import urljoin

    sitemap_cap = max_sitemaps or _MAX_SITEMAPS
    url_cap = max_urls or _MAX_URLS
    root_host = host_of(root_url)
    queue = list(
        dict.fromkeys(
            [*robots.sitemaps, urljoin(root_url, "/sitemap.xml"),
             urljoin(root_url, "/sitemap_index.xml"), urljoin(root_url, "/sitemap-index.xml")]
        )
    )
    seen_sitemaps: set[str] = set()
    found: dict[str, None] = {}

    while queue and len(seen_sitemaps) < sitemap_cap and len(found) < url_cap:
        batch = [u for u in queue[:5] if u not in seen_sitemaps]
        queue = queue[5:]
        if not batch:
            continue
        seen_sitemaps.update(batch)
        # A missing sitemap.xml is the common case, not an error worth retrying.
        for result in await fetcher.get_many(batch, attempts=1):
            if not result.ok or not result.text:
                continue
            pages, nested = parse_sitemap(result.text)
            for page in pages:
                url = canonicalize(page, base=root_url)
                if url and same_registrable_domain(host_of(url), root_host):
                    found.setdefault(url, None)
            for child in nested:
                url = canonicalize(child, base=root_url)
                if url and url not in seen_sitemaps:
                    queue.append(url)

    log.info(
        "sitemap discovery: %d urls from %d sitemaps for %s",
        len(found), len(seen_sitemaps), root_host,
    )
    return list(found)


_LINK_RE = re.compile(r'href=["\']([^"\'>]+)["\']', re.IGNORECASE)


def extract_links(
    html: str,
    base_url: str,
    *,
    same_domain_only: bool = True,
    allowed_domains: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Anchor hrefs from a page, canonicalized and domain-filtered.

    Hrefs are HTML-unescaped first. An attribute spells its separators as
    `&amp;`, and canonicalizing that literally turns `?a=1&amp;b=2` into a query
    with a parameter actually named `amp;b` — a different URL from the real one,
    and a different one again for every link to the same page. One faculty
    directory was fetched six times that way, each time for the same 20 people.
    """
    root_host = host_of(base_url)
    allowed = allowed_domains or {registrable_domain(root_host)}
    out: dict[str, None] = {}
    for raw_href in _LINK_RE.findall(html or ""):
        url = canonicalize(unescape(raw_href), base=base_url)
        if not url:
            continue
        if same_domain_only and not in_scope(host_of(url), allowed):
            continue
        out.setdefault(url, None)
    return list(out)


@dataclass(frozen=True)
class LinkContext:
    """A link as a person reading the page sees it, not just its href.

    The anchor text and the heading it sits under are what tell "Meet our
    interns" apart from "Apply now"; the URL alone often says neither.
    """

    url: str
    text: str = ""
    heading: str = ""
    in_nav: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


_CHROME_TAGS = frozenset({"nav", "header", "footer"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_SPACE = re.compile(r"\s+")


def extract_link_contexts(
    html: str,
    base_url: str,
    *,
    allowed_domains: set[str] | frozenset[str] | None = None,
) -> list[LinkContext]:
    """Every in-scope anchor with its text, nearest preceding heading, and
    whether it sits in site chrome. Hidden tabs and menus are included: a link
    that is not painted is still a link."""
    from selectolax.parser import HTMLParser

    root_host = host_of(base_url)
    allowed = allowed_domains or {registrable_domain(root_host)}
    tree = HTMLParser(html or "")
    root = tree.body or tree.root
    if root is None:
        return []

    out: dict[str, LinkContext] = {}
    heading = ""
    # (node, chrome_depth) in document order, iteratively: CMS markup nests too
    # deeply for recursion.
    stack: list[tuple[object, bool]] = [(root, False)]
    while stack:
        node, in_chrome = stack.pop()
        tag = node.tag  # type: ignore[attr-defined]
        if tag in ("script", "style", "noscript", "svg", "-text", "-comment"):
            continue
        if tag in _HEADING_TAGS:
            heading = _SPACE.sub(" ", node.text(separator=" ")).strip()[:120]  # type: ignore[attr-defined]
        if tag == "a":
            raw_href = node.attributes.get("href") or ""  # type: ignore[attr-defined]
            url = canonicalize(unescape(raw_href.strip()), base=base_url) if raw_href else None
            if url and in_scope(host_of(url), allowed):
                text = _SPACE.sub(" ", node.text(separator=" ")).strip()  # type: ignore[attr-defined]
                if not text:
                    img = node.css_first("img")  # type: ignore[attr-defined]
                    text = (
                        node.attributes.get("aria-label")  # type: ignore[attr-defined]
                        or node.attributes.get("title")  # type: ignore[attr-defined]
                        or (img.attributes.get("alt") if img is not None else "")
                        or ""
                    ).strip()
                existing = out.get(url)
                # Keep the most informative sighting: body text beats a menu entry.
                if existing is None or (existing.in_nav and not in_chrome) or (
                    not existing.text and text
                ):
                    out[url] = LinkContext(
                        url=url, text=text[:160], heading=heading, in_nav=in_chrome
                    )
            continue
        child_chrome = in_chrome or tag in _CHROME_TAGS
        children = list(node.iter())  # type: ignore[attr-defined]
        for child in reversed(children):
            stack.append((child, child_chrome))
    return list(out.values())
