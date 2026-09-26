"""Crawl-first verification: settle a record from its source page alone.

The crawl already labelled every person. Verification re-reads that same
source page and checks, without a model, whether the fresh read agrees with
the crawl: the name is still printed there, the person's own entry or the
section it sits under names the stored role, and nothing around it suggests
the page is about something other than who currently holds that role.

When all of that holds, the record is verified on the page's own words and no
model call is made. Only when the evidence conflicts or looks questionable is
the person handed to the model as a tiebreaker. A name printed in an article,
a nominee list, an alumni or former-resident page is never enough by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..llm.reader import fold, name_in_text
from ..llm.role_evidence import ROLE_EVIDENCE

# The stored role agrees with the page; the page no longer prints the name;
# or something needs a closer look.
Verdict = Literal["agree", "absent", "ambiguous"]

# Lines after the name line that may belong to that person's entry (title,
# PGY year, medical school) - or to the next person's. A heading ends it sooner.
_ENTRY_LINES = 2
# How far back to look for the section heading a name sits under.
_HEADING_LOOKBACK = 40

# A source that reads as news or an announcement, not a roster: a name there
# says the person was written about, not that they hold the role now.
_ARTICLE_PAGE = (
    "/news", "/blog", "/article", "/story", "/stories", "press-release",
    "spotlight", "award", "nominee", "nomination", "newsletter", "memoriam", "obituar",
)
_SUSPECT_CONTEXT = re.compile(
    r"\b(nominees?|nominated|nominations?|awards?|awarded|awardees?|honorees?|"
    r"congratulat\w*|in memoriam|obituary|news|announc\w*|spotlight|"
    r"match (day|list|results)|matched|incoming|graduat\w*|applicants?|interview(s|ees?)?)\b",
    re.IGNORECASE,
)
# A printed title that cannot belong to a current trainee.
_NOT_TRAINEE_TITLE = re.compile(
    r"\b(professor|attending|faculty|director|coordinator|alumni|alumnus|alumna|former)\b",
    re.IGNORECASE,
)
_TRAINEE = ("resident", "fellow")


@dataclass(frozen=True)
class CrawlCheck:
    verdict: Verdict
    reason: str
    # The line on the page that supports the stored role, when it agrees.
    evidence: str | None = None


def _roles_in(text: str, *, category: str) -> set[str]:
    """Every role the regex evidence finds in `text`.

    A fellow's entry commonly prints a PGY year ("Cardiology Fellow, PGY-5");
    that is not evidence of a second, residency role.
    """
    found = set()
    for role, pattern in ROLE_EVIDENCE.items():
        matches = [m.group(0).casefold() for m in pattern.finditer(text)]
        if role == "resident" and category == "fellow":
            matches = [m for m in matches if not m.startswith("pgy")]
        if matches:
            found.add(role)
    return found


def _heading_above(lines: list[str], index: int) -> str:
    for prior in range(index, max(-1, index - _HEADING_LOOKBACK), -1):
        if lines[prior].lstrip().startswith("#"):
            return lines[prior].strip().lstrip("#").strip()
    return ""


def _entry(lines: list[str], index: int) -> str:
    out = [lines[index]]
    for line in lines[index + 1 : index + 1 + _ENTRY_LINES]:
        if line.lstrip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out)


def _article_page(url: str, title: str) -> bool:
    page = f"{url} {title}".casefold()
    return any(token in page for token in _ARTICLE_PAGE)


def check_against_crawl(
    *, text: str, url: str, title: str, full_name: str, category: str,
    position: str | None = None,
) -> CrawlCheck:
    """Whether a fresh read of the source agrees with the crawl's label."""
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if name_in_text(full_name, fold(line))]
    if not hits:
        return CrawlCheck("absent", "The name no longer appears on the source page.")
    if category not in ROLE_EVIDENCE:
        return CrawlCheck("ambiguous", f"The crawl stored no checkable role ({category}).")
    if _article_page(url, title):
        return CrawlCheck("ambiguous", "The source looks like an article or announcement, not a roster.")
    if category in _TRAINEE and position and _NOT_TRAINEE_TITLE.search(position):
        return CrawlCheck("ambiguous", "The printed position conflicts with a current trainee role.")

    support: str | None = None
    # Every place the name is printed must be clean: a roster entry plus a
    # mention under "Former Residents" is a conflict, not a confirmation.
    for index in hits:
        heading = _heading_above(lines, index)
        # A page with no headings is one section, named by its title.
        section = heading or title
        entry = _entry(lines, index)
        context = f"{section}\n{entry}"
        if _SUSPECT_CONTEXT.search(context):
            return CrawlCheck("ambiguous", "The name appears in an announcement, award or match context.")
        if category != "alumni" and ROLE_EVIDENCE["alumni"].search(context):
            return CrawlCheck("ambiguous", "The name appears in an alumni or former-member context.")
        # The lines after the name may already be the next person's entry,
        # so they can raise a conflict but never supply the support.
        name_line = lines[index].strip()
        if _roles_in(entry, category=category) - {category}:
            return CrawlCheck("ambiguous", "The person's entry names a different role.")
        if category in _roles_in(name_line, category=category):
            support = support or (f"{section}: {name_line}" if section else name_line)
            continue
        section_roles = _roles_in(section, category=category)
        if section_roles == {category}:
            support = support or f"{section}: {name_line}"
            continue
        if section_roles:
            return CrawlCheck("ambiguous", "The section this name sits under names more than one role.")
    if support is None:
        return CrawlCheck("ambiguous", "The page prints the name but does not state the stored role near it.")
    return CrawlCheck(
        "agree",
        "The crawl's role and a fresh read of the same source agree.",
        evidence=support[:300],
    )
