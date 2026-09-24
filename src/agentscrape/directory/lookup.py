"""Search the directory for one person and read back one unambiguous match.

Cheap first: the HTML extractor reads the result page, and the model is asked
only when that does not produce a single match with something to add. If the
result is a list that links to a profile, the one profile link that names the
person is followed. A result naming two people who could be the target is
ambiguous and is dropped — a wrong address is worse than none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..db.enums import FetchMode
from ..discovery.sitemap import extract_link_contexts
from ..domain.matching import normalize_name
from ..extraction.html_people import extract_people
from ..extraction.person import ExtractedPerson
from ..extraction.text import html_to_model_text
from ..llm.reader import combine_with_regex, fold, read_page
from ..pipeline.deps import PipelineDeps
from .learn import BROWSER, GET, fill_template, looks_like_login, names_the_person, split_name

log = logging.getLogger("agentscrape.directory.lookup")

# A JSON search API can return a lot; the model reads this much of it.
MAX_JSON_CHARS = 20_000


@dataclass
class LookupResult:
    person: ExtractedPerson | None
    url: str
    title: str = ""
    fetch_mode: FetchMode = FetchMode.HTML
    # Why nothing came back, for the log and the counts.
    reason: str = ""


def same_person(a: str | None, b: str | None) -> bool:
    """Same first and last name once credentials, titles and middle initials
    are folded away."""
    left, right = normalize_name(a), normalize_name(b)
    if not left or not right:
        return False
    x, y = left.split(), right.split()
    return x[0] == y[0] and x[-1] == y[-1]


def unique_match(people: list[ExtractedPerson], full_name: str) -> ExtractedPerson | bool | None:
    """The one person on the page who is `full_name`; None if nobody is, and
    False if more than one could be."""
    matches: dict[str, ExtractedPerson] = {}
    for person in people:
        if same_person(person.full_name, full_name):
            matches.setdefault(person.email or f"name:{len(matches)}", person)
    if not matches:
        return None
    if len(matches) > 1:
        return False
    return next(iter(matches.values()))


def _adds_something(person: ExtractedPerson) -> bool:
    return bool(person.email or person.pgy is not None or person.class_of or person.position)


async def _read(deps: PipelineDeps, url: str, title: str, body: str, is_html: bool,
                full_name: str) -> ExtractedPerson | bool | None:
    """Who on this page is the person: regex first, then the model."""
    regex_people = extract_people(body, page_title=title, url=url) if is_html else []
    found = unique_match(regex_people, full_name)
    if found is False or (found is not None and _adds_something(found) and found.email):
        return found
    text = html_to_model_text(body) if is_html else body[:MAX_JSON_CHARS]
    reading = await read_page(url=url, title=title, text=text, meter=deps.meter)
    if not reading.ok:
        return found
    people = combine_with_regex(reading.people, regex_people, fold(text))
    return unique_match(people, full_name)


async def lookup_person(deps: PipelineDeps, config: dict, full_name: str) -> LookupResult:
    if config.get("mode") == GET:
        url = fill_template(config["template"], full_name)
        result = await deps.fetcher.get(url, attempts=2)
        if not result.ok:
            return LookupResult(None, url, reason=f"search failed ({result.error or result.status})")
        body, final, fetch_mode = result.text, result.final_url or url, FetchMode.HTML
        is_html = result.is_html
    elif config.get("mode") == BROWSER and deps.can_render:
        from ..browser.renderer import search_in_browser

        first, last = split_name(full_name)
        values = {"query": full_name, "first": first, "last": last}
        rendered = await search_in_browser(
            deps.browser_context, config["url"],
            {role: (selector, values[role]) for role, selector in config.get("fields", {}).items()},
        )
        if not rendered.ok:
            return LookupResult(None, config["url"], reason=f"search failed ({rendered.error})")
        body, final, fetch_mode, is_html = rendered.html, rendered.final_url, FetchMode.RENDER, True
    else:
        return LookupResult(None, "", reason="directory cannot be searched")

    if looks_like_login(final, body if is_html else ""):
        return LookupResult(None, final, reason="directory requires signing in")

    if not names_the_person(body, full_name, int(config.get("echo", 0))):
        return LookupResult(None, final, reason="not listed")

    from ..pipeline.nodes.extract import _title_of

    title = _title_of(body) if is_html else ""
    found = await _read(deps, final, title, body, is_html, full_name)
    if found is False:
        return LookupResult(None, final, title, fetch_mode, reason="ambiguous")

    # A result list that links to the person's profile: the profile is where
    # the address and year usually are.
    if is_html and (found is None or not found.email):
        profile = _profile_link(body, final, full_name)
        if profile:
            page = await deps.fetcher.get(profile, attempts=2)
            if page.ok and page.is_html:
                profile_title = _title_of(page.text)
                on_profile = await _read(
                    deps, page.final_url or profile, profile_title, page.text, True, full_name
                )
                if isinstance(on_profile, ExtractedPerson):
                    return LookupResult(on_profile, page.final_url or profile, profile_title)
    if found is None:
        return LookupResult(None, final, title, fetch_mode, reason="listed but not read")
    return LookupResult(found, final, title, fetch_mode)


def _profile_link(html: str, base_url: str, full_name: str) -> str | None:
    """The single link on the page whose text is this person's name."""
    links = {
        link.url for link in extract_link_contexts(html, base_url)
        if link.text and same_person(link.text, full_name)
    }
    return links.pop() if len(links) == 1 else None
