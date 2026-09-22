"""Work out how to search a school's people directory, once.

The cheapest outcome, and the common one, is a plain GET search: the
directory's own `<form>` names its action and its query field, so every later
lookup is one HTTP fetch of `action?field=<name>`. When there is no usable GET
form (a POST form, or a JavaScript app), the browser fills the search box once
and watches the address bar and the page's own requests: if the query shows up
in either, that URL becomes the template and lookups are plain fetches again.
Only when neither works is every lookup done in the browser.

A template is accepted only after a probe: searching for someone already known
at the school must return a page that names them. That keeps a site-wide
"search this site" box in the header from being mistaken for the directory.
Most search pages repeat the query back ("Search for Jane Doe", the box's own
value), so a made-up name is searched first to count those echoes, and a real
name counts as found only when it appears more often than that.

The result is a small JSON config stored on the site:
    {"mode": "get", "template": "https://.../search?q={query}", "echo": 1}
    {"mode": "get", "template": "...?first={first}&last={last}"}
    {"mode": "browser", "url": ..., "fields": {"query": "#search"}}
    {"mode": "unavailable", "reason": "..."}
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlsplit

from selectolax.parser import HTMLParser, Node

from ..llm.reader import fold
from ..pipeline.deps import PipelineDeps

log = logging.getLogger("agentscrape.directory")

GET = "get"
BROWSER = "browser"
UNAVAILABLE = "unavailable"

_LOGIN_URL = re.compile(
    r"(^|[/.?&=_-])(login|logon|signin|sign-in|sso|shibboleth|idp|cas|saml|oauth|adfs|auth)"
    r"([/.?&=_-]|$)",
    re.IGNORECASE,
)
_SEARCHY = re.compile(r"name|people|person|directory|search|query|keyword|term|lookup|^q$|find", re.IGNORECASE)
_FIRST = re.compile(r"first|given|fname", re.IGNORECASE)
_LAST = re.compile(r"last|family|surname|lname", re.IGNORECASE)
_DIRECTORY_FORM = re.compile(r"directory|people|person|staff|student|faculty|find", re.IGNORECASE)
_TEXT_TYPES = {"", "text", "search"}
_MARK = {"query": "QQQUERYQQ", "first": "QQFIRSTQQ", "last": "QQLASTQQ"}
# Nobody is called this; searching for it shows how often a page echoes a query.
CONTROL_NAME = "Qazwix Vornebly"


@dataclass(frozen=True)
class SearchForm:
    """A form that can search by name: its action, method, fixed fields, and
    which inputs take the query (one `query` input, or `first` and `last`)."""

    action: str
    method: str
    fixed: tuple[tuple[str, str], ...]
    fields: dict[str, str]  # role ("query" | "first" | "last") -> input name
    selectors: dict[str, str]  # role -> CSS selector, for the browser
    score: float

    def template(self) -> str:
        params = list(self.fixed) + [(name, _MARK[role]) for role, name in self.fields.items()]
        url = self.action.split("#")[0]
        sep = "&" if urlsplit(url).query else "?"
        built = f"{url}{sep}{urlencode(params)}"
        for role, mark in _MARK.items():
            built = built.replace(mark, "{" + role + "}")
        return built


def looks_like_login(url: str, html: str) -> bool:
    """A single sign-on redirect or a password field: the directory is private."""
    parts = urlsplit(url)
    if _LOGIN_URL.search(parts.netloc.split(".")[0]) or _LOGIN_URL.search(parts.path):
        return True
    tree = HTMLParser(html or "")
    return tree.css_first('input[type="password"]') is not None


def _hints(node: Node, tree: HTMLParser) -> str:
    attrs = node.attributes
    words = [attrs.get(k) or "" for k in ("name", "id", "placeholder", "aria-label", "title")]
    node_id = attrs.get("id")
    if node_id:
        label = tree.css_first(f'label[for="{node_id}"]')
        if label is not None:
            words.append(label.text(strip=True))
    return " ".join(words)


def _in_chrome(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag in ("header", "nav", "footer"):
            return True
        role = (parent.attributes.get("role") or "").lower()
        if role in ("banner", "navigation"):
            return True
        parent = parent.parent
    return False


def _selector(node: Node) -> str | None:
    attrs = node.attributes
    if attrs.get("id"):
        return f'[id="{attrs["id"]}"]'
    if attrs.get("name"):
        return f'input[name="{attrs["name"]}"]'
    return None


def find_search_forms(html: str, base_url: str) -> list[SearchForm]:
    """Every form with a name-search input, best first."""
    tree = HTMLParser(html or "")
    forms: list[SearchForm] = []
    for form in tree.css("form"):
        attrs = form.attributes
        inputs = [
            node for node in form.css("input")
            if (node.attributes.get("type") or "").lower() in _TEXT_TYPES
        ]
        named = [n for n in inputs if n.attributes.get("name")]
        if not named:
            continue
        first = next((n for n in named if _FIRST.search(_hints(n, tree))), None)
        last = next((n for n in named if _LAST.search(_hints(n, tree))), None)
        if first is not None and last is not None and first.mem_id != last.mem_id:
            chosen = {"first": first, "last": last}
        else:
            ranked = sorted(named, key=lambda n: bool(_SEARCHY.search(_hints(n, tree))), reverse=True)
            chosen = {"query": ranked[0]}

        fixed: list[tuple[str, str]] = []
        chosen_ids = {n.mem_id for n in chosen.values()}
        for node in form.css("input"):
            kind = (node.attributes.get("type") or "").lower()
            name = node.attributes.get("name")
            if not name or node.mem_id in chosen_ids:
                continue
            if kind == "hidden":
                fixed.append((name, node.attributes.get("value") or ""))
            elif kind in ("checkbox", "radio") and "checked" in node.attributes:
                fixed.append((name, node.attributes.get("value") or "on"))
        for select in form.css("select"):
            name = select.attributes.get("name")
            if not name:
                continue
            option = select.css_first("option[selected]") or select.css_first("option")
            if option is not None:
                fixed.append((name, option.attributes.get("value") or option.text(strip=True)))

        form_words = " ".join(attrs.get(k) or "" for k in ("action", "id", "class", "name", "aria-label"))
        score = 0.0
        score += 3 if any(_SEARCHY.search(_hints(n, tree)) for n in chosen.values()) else 0
        score += 2 if _DIRECTORY_FORM.search(form_words) else 0
        score += 2 if "first" in chosen else 0
        score -= 4 if _in_chrome(form) else 0
        selectors = {role: _selector(n) for role, n in chosen.items()}
        forms.append(SearchForm(
            action=urljoin(base_url, attrs.get("action") or base_url),
            method=(attrs.get("method") or "get").lower(),
            fixed=tuple(fixed),
            fields={role: n.attributes["name"] for role, n in chosen.items()},
            selectors={k: v for k, v in selectors.items() if v},
            score=score,
        ))
    return sorted(forms, key=lambda f: f.score, reverse=True)


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    return (parts[0], parts[-1]) if len(parts) > 1 else (full_name, full_name)


def fill_template(template: str, full_name: str) -> str:
    first, last = split_name(full_name)
    return (
        template.replace("{query}", quote_plus(full_name))
        .replace("{first}", quote_plus(first))
        .replace("{last}", quote_plus(last))
    )


def echo_count(text: str, full_name: str = CONTROL_NAME) -> int:
    """How many times the page repeats a searched name's rarer half."""
    first, last = split_name(full_name)
    folded = fold(text)
    return min(folded.count(fold(first)), folded.count(fold(last)))


def names_the_person(text: str, full_name: str, echo: int = 0) -> bool:
    """The result page mentions this person by first and last name more often
    than it merely repeats the query back."""
    return echo_count(text, full_name) > echo


async def _probe(deps: PipelineDeps, template: str, probes: list[str]) -> int | None:
    """Search for people already known at the school; any hit confirms the
    template. Returns the page's echo count, or None when nobody was found."""
    control = await deps.fetcher.get(fill_template(template, CONTROL_NAME), attempts=2)
    if not control.ok:
        return None
    echo = echo_count(control.text)
    for name in probes[:3]:
        result = await deps.fetcher.get(fill_template(template, name), attempts=2)
        if result.ok and names_the_person(result.text, name, echo):
            return echo
    return None


def _template_from_url(url: str, full_name: str) -> str | None:
    """Turn a URL the search produced into a template, if the query is in it."""
    first, last = split_name(full_name)
    for encode in (quote_plus, quote):
        whole = encode(full_name)
        if whole in url:
            return url.replace(whole, "{query}")
        if encode(first) in url and encode(last) in url and first != last:
            return url.replace(encode(first), "{first}", 1).replace(encode(last), "{last}", 1)
    return None


async def learn_directory(
    deps: PipelineDeps, directory_url: str, probes: list[str]
) -> dict:
    """How to search this directory. `probes` are names known to be at the
    school, used to confirm a template actually finds people."""
    config = await _learn(deps, directory_url, probes)
    config["learned_at"] = datetime.now(UTC).isoformat()
    config["directory_url"] = directory_url
    log.info("directory %s: %s", directory_url, {k: v for k, v in config.items() if k != "learned_at"})
    return config


async def _learn(deps: PipelineDeps, directory_url: str, probes: list[str]) -> dict:
    if not probes:
        return {"mode": UNAVAILABLE, "reason": "no named people to search for"}
    page = await deps.fetcher.get(directory_url, attempts=2)
    html = page.text if page.ok and page.is_html else ""
    final = page.final_url or directory_url
    if looks_like_login(final, html):
        return {"mode": UNAVAILABLE, "reason": f"the directory requires signing in ({final})"}

    forms = find_search_forms(html, final)
    for form in forms:
        if form.method != "get":
            continue
        echo = await _probe(deps, form.template(), probes)
        if echo is not None:
            return {"mode": GET, "template": form.template(), "echo": echo}

    if not deps.can_render:
        reason = "no search form that returns people" if html else f"could not load it ({page.error or page.status})"
        return {"mode": UNAVAILABLE, "reason": reason}

    from ..browser.renderer import render_page, search_in_browser

    rendered = await render_page(deps.browser_context, directory_url)
    if not rendered.ok:
        return {"mode": UNAVAILABLE, "reason": f"could not load it ({rendered.error})"}
    if looks_like_login(rendered.final_url, rendered.html):
        return {"mode": UNAVAILABLE, "reason": f"the directory requires signing in ({rendered.final_url})"}
    rendered_forms = [f for f in find_search_forms(rendered.html, rendered.final_url) if f.selectors]
    if not rendered_forms:
        # A search box outside any <form>, as JavaScript apps often have.
        rendered_forms = _loose_inputs(rendered.html, rendered.final_url)
    for form in rendered_forms[:2]:
        async def search(name: str, form: SearchForm = form):
            first, last = split_name(name)
            values = {"query": name, "first": first, "last": last}
            return await search_in_browser(
                deps.browser_context, directory_url,
                {role: (selector, values[role]) for role, selector in form.selectors.items()},
            )

        control = await search(CONTROL_NAME)
        if not control.ok:
            continue
        browser_echo = echo_count(control.html)
        name = probes[0]
        result = await search(name)
        if not result.ok or not names_the_person(result.html, name, browser_echo):
            continue
        for candidate in [result.final_url, *result.requests]:
            template = _template_from_url(candidate, name)
            if template:
                echo = await _probe(deps, template, probes)
                if echo is not None:
                    return {"mode": GET, "template": template, "echo": echo}
        return {"mode": BROWSER, "url": directory_url, "fields": form.selectors,
                "echo": browser_echo}
    return {"mode": UNAVAILABLE, "reason": "no search box that returns people"}


def _loose_inputs(html: str, base_url: str) -> list[SearchForm]:
    """Search-like text inputs that sit outside any form."""
    tree = HTMLParser(html or "")
    out = []
    for node in tree.css("input"):
        if (node.attributes.get("type") or "").lower() not in _TEXT_TYPES:
            continue
        hints = _hints(node, tree)
        selector = _selector(node)
        if selector and _SEARCHY.search(hints) and not _in_chrome(node):
            out.append(SearchForm(base_url, "js", (), {"query": node.attributes.get("name") or ""},
                                  {"query": selector}, 1.0))
    return out
