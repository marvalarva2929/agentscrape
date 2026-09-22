#!/usr/bin/env python
"""Can every assigned school be reached and read? No model calls, no database.

For each school in data/schools-23.csv this loads its entry page, home page and
program index twice — over plain HTTP and in Chromium — and reports what a crawl
would meet: the status, whether the page is a challenge or a block page, whether
it only exists once JavaScript has run, how much text it holds, how many people
the rule-based parser reads out of it, iframes and PDFs the crawler cannot read,
and the other sites its pages send people to for residents and fellows (the
evidence for `affiliated_domains`).

    .venv/bin/python scripts/preflight_schools.py [--only tower,lvhn] [--out data/preflight-report]

Costs nothing but time. Writes <out>.json and <out>.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentscrape.browser.fetcher import Fetcher  # noqa: E402
from agentscrape.browser.renderer import BrowserPool, render_page  # noqa: E402
from agentscrape.extraction.html_people import extract_people, page_looks_thin  # noqa: E402
from agentscrape.extraction.text import html_to_model_text  # noqa: E402
from agentscrape.school_catalog import CatalogSchool, load_catalog  # noqa: E402
from agentscrape.urls import host_of, registrable_domain  # noqa: E402
from agentscrape.validation.institution import classify_domain  # noqa: E402

# Wording a bot-protection or access-denied interstitial uses instead of content.
CHALLENGE = re.compile(
    r"just a moment|attention required|access denied|verify you are (a )?human|"
    r"checking your browser|are you a robot|request blocked|unusual traffic|"
    r"enable javascript and cookies|pardon our interruption|incapsula|perimeterx",
    re.IGNORECASE,
)
ROSTER_WORDS = re.compile(r"resident|fellow|program|training|gme|graduate medical", re.IGNORECASE)
MIN_USEFUL_CHARS = 1500


@dataclass
class PageCheck:
    label: str
    url: str
    http_status: int | None = None
    http_final: str = ""
    http_error: str | None = None
    http_chars: int = 0
    http_people: int = 0
    browser_status: int | None = None
    browser_final: str = ""
    browser_error: str | None = None
    browser_chars: int = 0
    browser_people: int = 0
    challenge: str = ""
    js_only: bool = False
    thin_reason: str = ""
    iframes: int = 0
    pdf_links: int = 0
    redirected_to_other_domain: bool = False


@dataclass
class SchoolReport:
    name: str
    host: str
    verdict: str = ""
    notes: list[str] = field(default_factory=list)
    domain_verdict: str = ""
    pages: list[PageCheck] = field(default_factory=list)
    outlinks: dict[str, int] = field(default_factory=dict)


def _challenge(title: str, text: str) -> str:
    sample = f"{title}\n{text[:1500]}"
    match = CHALLENGE.search(sample)
    return match.group(0) if match and len(text.strip()) < 4000 else ""


async def check_page(
    fetcher: Fetcher, context, school: CatalogSchool, label: str, url: str, census: Counter
) -> PageCheck:
    check = PageCheck(label=label, url=url)
    own = {registrable_domain(school.host), *school.scope_domains}

    result = await fetcher.get(url, attempts=2)
    check.http_status, check.http_final, check.http_error = result.status, result.final_url, result.error
    http_html = result.text if result.ok else ""
    if http_html:
        text = html_to_model_text(http_html)
        check.http_chars = len(text)
        check.http_people = len(extract_people(http_html, url=result.final_url))
        check.iframes = len(re.findall(r"<iframe\b", http_html, re.IGNORECASE))
        check.pdf_links = len(re.findall(r'href="[^"]+\.pdf(?:[?#][^"]*)?"', http_html, re.IGNORECASE))
        check.challenge = _challenge("", text)

    rendered = await render_page(context, url)
    check.browser_status, check.browser_final, check.browser_error = rendered.status, rendered.final_url, rendered.error
    if rendered.ok:
        check.browser_chars = len(rendered.text or "")
        check.browser_people = len(extract_people(rendered.html, page_title=rendered.title, url=rendered.final_url))
        check.challenge = check.challenge or _challenge(rendered.title, rendered.text or "")
        check.iframes = max(check.iframes, len(re.findall(r"<iframe\b", rendered.html, re.IGNORECASE)))
        check.js_only = check.browser_chars >= MIN_USEFUL_CHARS and check.http_chars < 400
        _, check.thin_reason = page_looks_thin(rendered.html, rendered.text or "", check.browser_people)
        for link in rendered.links:
            domain = registrable_domain(host_of(link["url"]))
            if domain and domain not in own and ROSTER_WORDS.search(f"{link['text']} {link['context'][:120]}"):
                census[domain] += 1
        final_domain = registrable_domain(host_of(rendered.final_url))
        check.redirected_to_other_domain = bool(final_domain) and final_domain not in own
    return check


def judge(report: SchoolReport) -> None:
    entry = next((p for p in report.pages if p.label == "entry"), None)
    if entry is None:
        report.verdict = "NO ENTRY"
        return
    blocked = bool(entry.challenge) or entry.http_status in (401, 403, 429) and entry.browser_status in (None, 401, 403, 429)
    readable = max(entry.http_chars, entry.browser_chars) >= MIN_USEFUL_CHARS
    reached = entry.http_status == 200 or entry.browser_status == 200
    if not reached:
        report.verdict = "UNREACHABLE"
        report.notes.append(entry.http_error or entry.browser_error or "no response")
    elif blocked:
        report.verdict = "BLOCKED"
        report.notes.append(f"challenge/denied page ({entry.challenge or entry.http_status})")
    elif not readable:
        report.verdict = "THIN"
        report.notes.append(f"little text ({entry.browser_chars} chars rendered)")
    elif entry.js_only:
        report.verdict = "OK (needs browser)"
        report.notes.append("content only appears after JavaScript runs")
    else:
        report.verdict = "OK"
    if entry.redirected_to_other_domain:
        report.notes.append(f"entry redirects to {registrable_domain(host_of(entry.browser_final or entry.http_final))}")
    if entry.iframes:
        report.notes.append(f"{entry.iframes} iframe(s) on entry")
    if entry.pdf_links:
        report.notes.append(f"{entry.pdf_links} PDF link(s) on entry")
    home = next((p for p in report.pages if p.label == "homepage"), None)
    if report.verdict in ("BLOCKED", "UNREACHABLE", "THIN") and home and max(home.http_chars, home.browser_chars) >= MIN_USEFUL_CHARS:
        report.notes.append("homepage is readable, so a crawl can fall back to it")


async def preflight(school: CatalogSchool, fetcher: Fetcher, pool: BrowserPool, slot: str) -> SchoolReport:
    report = SchoolReport(name=school.name, host=school.host)
    verdict = classify_domain(school.entry_url)
    report.domain_verdict = f"{verdict.status}" if verdict else "needs page content"
    context = await pool.acquire(slot)
    census: Counter = Counter()
    targets = [("entry", school.entry_url), ("homepage", school.homepage), ("program_index", school.program_index_url)]
    seen: set[str] = set()
    for label, url in targets:
        if url and url not in seen:
            seen.add(url)
            report.pages.append(await check_page(fetcher, context, school, label, url, census))
    report.outlinks = dict(census.most_common(6))
    judge(report)
    return report


def markdown(reports: list[SchoolReport]) -> str:
    lines = [
        f"# Preflight of the 23 schools — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "Entry page loaded over plain HTTP and in Chromium. *People* is what the rule-based parser reads with no model.",
        "",
        "| School | Verdict | HTTP | Browser | Text (http/browser) | People (http/browser) | Notes | Links out to |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        e = next((p for p in r.pages if p.label == "entry"), None)
        if e is None:
            lines.append(f"| {r.name} | {r.verdict} | | | | | | |")
            continue
        out = ", ".join(f"{d} ({n})" for d, n in r.outlinks.items())
        lines.append(
            f"| {r.name} | **{r.verdict}** | {e.http_status or e.http_error or '—'} | {e.browser_status or e.browser_error or '—'} "
            f"| {e.http_chars:,} / {e.browser_chars:,} | {e.http_people} / {e.browser_people} "
            f"| {'; '.join(r.notes)} | {out} |"
        )
    counts = Counter(r.verdict for r in reports)
    lines += ["", "**Summary:** " + ", ".join(f"{v} {n}" for v, n in counts.most_common()), ""]
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated words; keeps schools whose name or host contains one")
    parser.add_argument("--out", default="data/preflight-report")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    schools = load_catalog()
    if args.only:
        words = [w.strip().lower() for w in args.only.split(",") if w.strip()]
        schools = [s for s in schools if any(w in s.name.lower() or w in s.host for w in words)]
    print(f"preflight of {len(schools)} schools", flush=True)

    pool = BrowserPool(size=args.workers)
    await pool.start()
    queue: asyncio.Queue[CatalogSchool] = asyncio.Queue()
    for school in schools:
        queue.put_nowait(school)
    reports: list[SchoolReport] = []

    async with Fetcher(concurrency=4) as fetcher:
        async def worker(slot: str) -> None:
            while True:
                try:
                    school = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    report = await asyncio.wait_for(preflight(school, fetcher, pool, slot), timeout=240)
                except Exception as exc:  # one bad school must not stop the others
                    report = SchoolReport(name=school.name, host=school.host, verdict="ERROR", notes=[f"{type(exc).__name__}: {exc}"[:200]])
                reports.append(report)
                print(f"  {report.verdict:<20} {school.name}", flush=True)

        await asyncio.gather(*(worker(f"w{i}") for i in range(args.workers)))
    await pool.stop()

    order = {s.name: i for i, s in enumerate(schools)}
    reports.sort(key=lambda r: order.get(r.name, 0))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps([asdict(r) for r in reports], indent=2))
    out.with_suffix(".md").write_text(markdown(reports))
    print(f"wrote {out.with_suffix('.md')} and .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
