#!/usr/bin/env python
"""Measure a scrape against a client's target sheet.

The client's working spreadsheet is the ground truth for a school: if a person
is on it and not in the database, the crawl missed a page. Coverage grouped by
specialty is what makes the failure legible — a whole specialty missing means a
roster page was never visited, while a handful missing across many specialties
means extraction dropped them off pages that were.

    uv run python scripts/coverage_check.py medicine.arizona.edu ~/target.csv

The sheet needs `Full Name` and `Email` columns; `Specialty` is used for the
grouping when present. Matching is by address first, then by first and last
name, so a nickname or a stripped credential does not read as a miss.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

_CREDENTIALS = re.compile(
    r"\b(m\.?d|d\.?o|ph\.?d|mbbs|mbbch|mph|msc?|mba|rn|np|pa-?c|do|md)\b"
)
_NOT_NAME = re.compile(r"[^a-z ]")


def fold(name: str) -> str:
    """Lowercase, strip accents, credentials and anything after a comma."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.split(",")[0]
    text = _NOT_NAME.sub(" ", _CREDENTIALS.sub(" ", text))
    return " ".join(w for w in text.split() if len(w) > 1)


def first_last(name: str) -> tuple[str, str] | None:
    """The two parts a sheet and a web page reliably agree on."""
    words = fold(name).split()
    return (words[0], words[-1]) if len(words) >= 2 else None


async def scraped(host: str) -> list[dict[str, str]]:
    from sqlalchemy import select

    from agentscrape.db.models import Record, Site
    from agentscrape.db.session import dispose_engine, get_sessionmaker

    async with get_sessionmaker()() as session:
        site_id = await session.scalar(select(Site.id).where(Site.root_domain == host))
        if site_id is None:
            raise SystemExit(f"no site in the database for {host!r}")
        rows = (
            await session.execute(
                select(Record.full_name, Record.email, Record.category).where(
                    Record.site_id == site_id
                )
            )
        ).all()
    await dispose_engine()
    return [{"name": r[0] or "", "email": r[1] or "", "category": r[2]} for r in rows]


def report(target: list[dict[str, str]], found: list[dict[str, str]]) -> int:
    emails = {f["email"].lower() for f in found if f["email"]}
    names = {k for f in found if (k := first_last(f["name"]))}

    missing = []
    by_email = by_name = 0
    for person in target:
        if person["email"] and person["email"].lower() in emails:
            by_email += 1
        elif (key := first_last(person["name"])) and key in names:
            by_name += 1
        else:
            missing.append(person)

    total = len(target)
    print(f"target rows          : {total}")
    print(f"records scraped      : {len(found)}")
    print(f"matched on address   : {by_email}")
    print(f"matched on name only : {by_name}")
    print(f"missing              : {len(missing)}")
    print(f"coverage             : {100 * (total - len(missing)) / total:.1f}%")

    if missing:
        totals = Counter(p["specialty"] for p in target)
        print("\nmissing by specialty (missing of total):")
        for specialty, count in Counter(
            p["specialty"] for p in missing
        ).most_common():
            print(f"  {count:4d} of {totals[specialty]:<4d} {specialty}")
        print("\nmissing people:")
        for person in missing:
            print(f"  {person['specialty'][:26]:28s} {person['name'][:28]:30s} {person['email']}")

    by_category = Counter(f["category"] for f in found)
    print("\nscraped by category:")
    for category, count in by_category.most_common():
        print(f"  {count:5d}  {category}")
    return 0 if not missing else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="the site's root domain, as stored")
    parser.add_argument("sheet", type=Path, help="the client's target CSV")
    args = parser.parse_args()

    with args.sheet.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "Full Name" not in rows[0]:
        raise SystemExit(f"{args.sheet} has no 'Full Name' column")
    target = [
        {
            "name": (r.get("Full Name") or "").strip(),
            "email": (r.get("Email") or "").strip(),
            "specialty": (r.get("Specialty") or "-").strip(),
        }
        for r in rows
        if (r.get("Full Name") or "").strip()
    ]
    return report(target, asyncio.run(scraped(args.host)))


if __name__ == "__main__":
    sys.exit(main())
