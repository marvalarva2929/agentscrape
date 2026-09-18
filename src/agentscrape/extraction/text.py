"""Turn a page into the text the model reads.

`innerText` and naive tag stripping both lose exactly what rosters hide in:

  * content in inactive tabs and collapsed accordions (in the HTML, not painted)
  * addresses that exist only as `mailto:` hrefs behind a "Email" link
  * names that exist only as a headshot's `alt` text
  * people rendered client-side from a JSON blob embedded in a <script>

So this walks the DOM itself, keeps block structure as line breaks and headings
as `## ` lines (the model uses them to tell a class year or a faculty section
from the trainees), and appends person-shaped fields from embedded JSON.
"""

from __future__ import annotations

import json
import re

from selectolax.parser import HTMLParser, Node

from ..domain.matching import normalize_email
from ..validation.email import deobfuscate

_SKIP = frozenset({"script", "style", "noscript", "svg", "nav", "footer", "iframe", "select"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCK = frozenset({
    "p", "div", "li", "tr", "section", "article", "ul", "ol", "table", "dd", "dt",
    "dl", "figure", "figcaption", "header", "main", "aside", "blockquote",
    "address", "br", "hr", "tbody", "thead", "details", "summary", "form",
    "fieldset", "legend", "caption", "body",
})
_CELLS = frozenset({"td", "th"})
_WS = re.compile(r"[ \t\r\f\v ]+")
_BLANKS = re.compile(r"\n{3,}")

# Keys in embedded JSON that carry a person's fields. Matched case-insensitively
# as a suffix/infix, so `displayFirstName`, `emailAddress`, `jobTitle` all count.
_PERSON_KEY = re.compile(
    r"(name|email|mail|title|position|role|specialt|degree|pgy|year|class|program|department)",
    re.IGNORECASE,
)
_JSON_PAIR = re.compile(r'"([A-Za-z_][A-Za-z0-9_]{0,40})"\s*:\s*"((?:[^"\\]|\\.){1,200})"')
_MAX_JSON_CHARS = 60_000


def html_to_model_text(html: str) -> str:
    """Readable, structure-preserving text for the model, hidden content included."""
    if not html or not html.strip():
        return ""
    tree = HTMLParser(deobfuscate(html))
    embedded = _embedded_people_json(tree)
    root = tree.body or tree.root
    if root is None:
        return embedded
    body = _walk(root)
    if embedded:
        body = f"{body}\n\n## Data embedded in the page's scripts\n{embedded}"
    return body


def _walk(root: Node) -> str:
    out: list[str] = []
    # An explicit stack rather than recursion: CMS pages nest deeply enough to
    # reach Python's recursion limit.
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, closing = stack.pop()
        tag = node.tag
        if closing:
            if tag == "a":
                email = _mailto(node)
                if email:
                    out.append(f" <{email}>")
            elif tag in _CELLS:
                out.append(" | ")
            elif tag in _BLOCK:
                out.append("\n")
            continue
        if tag == "-text":
            text = node.text(deep=False)
            if text:
                out.append(text)
            continue
        if tag in _SKIP or tag == "-comment":
            continue
        if tag in _HEADINGS:
            heading = _WS.sub(" ", node.text(separator=" ")).strip()
            if heading:
                out.append(f"\n## {heading}\n")
            continue
        if tag == "img":
            alt = (node.attributes.get("alt") or "").strip()
            if alt:
                out.append(f" [image: {alt}] ")
            continue
        if tag in _BLOCK:
            out.append("\n")
        stack.append((node, True))
        children = list(node.iter(include_text=True))
        for child in reversed(children):
            stack.append((child, False))
    return _tidy("".join(out))


def _mailto(node: Node) -> str | None:
    href = (node.attributes.get("href") or "").strip()
    if not href.lower().startswith("mailto:"):
        return None
    return normalize_email(href)


def _tidy(text: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    joined = "\n".join(line for line in lines)
    return _BLANKS.sub("\n\n", joined).strip()


def _embedded_people_json(tree: HTMLParser) -> str:
    """Person-shaped string fields from scripts, one record per line.

    A record starts over whenever a key repeats, which is how a flat scan of a
    serialized list of objects splits back into the objects.
    """
    records: list[list[str]] = []
    current: list[str] = []
    seen: set[str] = set()
    total = 0
    for script in tree.css("script"):
        source = script.text() or ""
        if '":' not in source:
            continue
        for key, raw in _JSON_PAIR.findall(source):
            if not _PERSON_KEY.search(key):
                continue
            try:
                value = json.loads(f'"{raw}"')
            except json.JSONDecodeError:
                value = raw
            value = _WS.sub(" ", value).strip()
            if not value or value.startswith(("http", "/")) or len(value) > 160:
                continue
            if key in seen:
                if current:
                    records.append(current)
                current, seen = [], set()
            seen.add(key)
            current.append(f"{key}={value}")
            total += len(key) + len(value) + 3
            if total > _MAX_JSON_CHARS:
                break
        if total > _MAX_JSON_CHARS:
            break
    if current:
        records.append(current)
    # A handful of fields is site configuration ("title=Home"), not a roster.
    useful = [r for r in records if len(r) >= 2]
    if len(useful) < 3:
        return ""
    return "\n".join("; ".join(r) for r in useful)


def chunk_text(text: str, size: int, overlap: int = 2_000) -> list[str]:
    """Split on line boundaries into chunks of at most about `size` characters.

    Consecutive chunks overlap so a person straddling a boundary is whole in at
    least one of them; duplicates are merged afterwards.
    """
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            newline = text.rfind("\n", start + size // 2, end)
            if newline != -1:
                end = newline
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        newline = text.find("\n", start, end)
        if newline != -1:
            start = newline + 1
    return chunks
