#!/usr/bin/env python
"""Score a scrape against the client's sheet for one institution, or all of them.

The client's sheet is the only external check on this crawler. It answers two
questions the run itself cannot:

  recall     did we find the people they already know about?
  labelling  did we call them what they are?

A person who is in the database but filed as `unknown` or `alumni` is not a
success — they will not appear in a resident-and-fellow export, which is the
product. So recall here means *found and correctly labelled*, and the two halves
are reported separately so a regression tells you which one moved.

Precision against the sheet is not directly measurable: the sheet covers part of
an institution, so someone we labelled a resident who is absent from it may be a
real resident the client has not collected yet. What is measurable, and what a
precision failure looks like, is the ratio of everyone we call a resident or
fellow to the institution's published trainee count. Far above 1.0 means we are
inventing trainees; far below means we are missing rosters.

    uv run python scripts/benchmark.py              # every institution
    uv run python scripts/benchmark.py bcm uchicago # named ones
    uv run python scripts/benchmark.py --misses bcm # list who we missed
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "benchmarks" / "institutions.json"
TRAINEE = {"resident", "fellow"}
_CREDENTIALS = re.compile(
    r"\b(m\.?d|d\.?o|ph\.?d|mbbs|mbbch|mph|msc?|mba|rn|np|pa-?c|do|md)\b"
)
_NOT_NAME = re.compile(r"[^a-z ]")


def fold(name: str) -> str:
    """Lowercase, strip accents, credentials and anything after a comma."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.split(",")[0]
    return " ".join(
        w for w in _NOT_NAME.sub(" ", _CREDENTIALS.sub(" ", text)).split() if len(w) > 1
    )


def first_last(name: str) -> tuple[str, str] | None:
    """The two parts a sheet and a web page reliably agree on."""
    words = fold(name).split()
    return (words[0], words[-1]) if len(words) >= 2 else None


@dataclass
class Score:
    slug: str
    name: str
    target: int = 0
    scraped_total: int = 0
    scraped_rf: int = 0
    found: int = 0
    labelled: int = 0
    rf_split_right: int = 0
    rf_split_known: int = 0
    expected_rf: int | None = None
    missing: list[dict] = field(default_factory=list)
    mislabelled: list[dict] = field(default_factory=list)

    @property
    def recall_found(self) -> float:
        return self.found / self.target if self.target else 0.0

    @property
    def recall_labelled(self) -> float:
        return self.labelled / self.target if self.target else 0.0

    @property
    def split_accuracy(self) -> float:
        return self.rf_split_right / self.rf_split_known if self.rf_split_known else 0.0

    @property
    def yield_ratio(self) -> float | None:
        """Our trainee count over the institution's published one."""
        if not self.expected_rf:
            return None
        return self.scraped_rf / self.expected_rf


async def scraped_records(host: str) -> list[dict[str, str]]:
    from sqlalchemy import select

    from agentscrape.db.models import Record, RecordVersion, Site
    from agentscrape.db.session import dispose_engine, get_sessionmaker

    async with get_sessionmaker()() as session:
        site_id = await session.scalar(select(Site.id).where(Site.root_domain == host))
        if site_id is None:
            await dispose_engine()
            return []
        rows = (
            await session.execute(
                select(
                    Record.full_name,
                    Record.email,
                    Record.category,
                    Record.position,
                    RecordVersion.source_url,
                )
                .outerjoin(RecordVersion, RecordVersion.id == Record.current_version_id)
                .where(Record.site_id == site_id)
            )
        ).all()
    await dispose_engine()
    return [
        {
            "name": r[0] or "",
            "email": (r[1] or "").lower(),
            "category": r[2],
            "position": r[3] or "",
            "source_url": r[4] or "",
        }
        for r in rows
    ]


def score_one(slug: str, cfg: dict, records: list[dict]) -> Score:
    target_rows = [
        r
        for r in csv.DictReader((ROOT / cfg["sheet"]).open(newline=""))
        if (r.get("Full Name") or "").strip()
    ]
    out = Score(
        slug=slug,
        name=cfg["name"],
        target=len(target_rows),
        scraped_total=len(records),
        scraped_rf=sum(1 for r in records if r["category"] in TRAINEE),
        expected_rf=cfg.get("expected_residents_fellows"),
    )

    by_email = {r["email"]: r for r in records if r["email"]}
    by_name: dict[tuple[str, str], dict] = {}
    for r in records:
        key = first_last(r["name"])
        # Prefer a trainee-labelled row when one person yielded several.
        if key and (key not in by_name or r["category"] in TRAINEE):
            by_name[key] = r

    for row in target_rows:
        email = (row.get("Email") or "").strip().lower()
        name = (row.get("Full Name") or "").strip()
        wanted = (row.get("R/F") or "").strip().upper()
        rec = (by_email.get(email) if email else None) or by_name.get(first_last(name))

        if rec is None:
            out.missing.append(
                {"name": name, "email": email, "specialty": row.get("Specialty", "")}
            )
            continue
        out.found += 1
        if rec["category"] in TRAINEE:
            out.labelled += 1
            if wanted.startswith("R") or wanted.startswith("F"):
                out.rf_split_known += 1
                expected = "resident" if wanted.startswith("R") else "fellow"
                if rec["category"] == expected:
                    out.rf_split_right += 1
        else:
            out.mislabelled.append(
                {
                    "name": name,
                    "want": wanted,
                    "got": rec["category"],
                    "source_url": rec["source_url"],
                }
            )
    return out


def print_scoreboard(scores: list[Score]) -> None:
    head = (
        f"{'institution':<22} {'sheet':>6} {'found':>13} {'labelled R/F':>14} "
        f"{'R vs F':>9} {'our R&F':>9} {'vs published':>13}"
    )
    print(head)
    print("-" * len(head))
    for s in scores:
        if not s.target:
            print(f"{s.slug:<22} {'no sheet':>6}")
            continue
        ratio = "" if s.yield_ratio is None else f"{s.yield_ratio * 100:.0f}%"
        print(
            f"{s.slug:<22} {s.target:>6} "
            f"{s.found:>6} {s.recall_found * 100:>5.1f}% "
            f"{s.labelled:>7} {s.recall_labelled * 100:>5.1f}% "
            f"{s.split_accuracy * 100:>8.0f}% "
            f"{s.scraped_rf:>9} {ratio:>13}"
        )
    done = [s for s in scores if s.target]
    if done:
        print("-" * len(head))
        total, labelled = sum(s.target for s in done), sum(s.labelled for s in done)
        print(f"{'overall':<22} {total:>6} {'':>13} {labelled:>7} {labelled / total * 100:>5.1f}%")


def print_failures(score: Score, limit: int) -> None:
    if score.missing:
        print(f"\n[{score.slug}] not found at all ({len(score.missing)}):")
        by_specialty = Counter(m["specialty"] for m in score.missing)
        for specialty, n in by_specialty.most_common(12):
            print(f"    {n:4d}  {specialty}")
        for m in score.missing[:limit]:
            print(f"      {m['name'][:30]:32s} {m['email']}")
    if score.mislabelled:
        print(f"\n[{score.slug}] found but not labelled resident/fellow "
              f"({len(score.mislabelled)}):")
        by_cause = Counter((m["got"], m["source_url"]) for m in score.mislabelled)
        for (got, url), n in by_cause.most_common(12):
            print(f"    {n:4d}  -> {got:8s} {url[:84]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slugs", nargs="*", help="institutions to score (default: all)")
    parser.add_argument("--misses", action="store_true", help="list the failures")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    config = json.loads(CONFIG.read_text())
    slugs = args.slugs or list(config)
    unknown = [s for s in slugs if s not in config]
    if unknown:
        raise SystemExit(f"unknown institution(s): {', '.join(unknown)}")

    scores = []
    for slug in slugs:
        cfg = config[slug]
        records = asyncio.run(scraped_records(cfg["host"]))
        scores.append(score_one(slug, cfg, records))

    print_scoreboard(scores)
    if args.misses:
        for s in scores:
            print_failures(s, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
