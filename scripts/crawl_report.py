#!/usr/bin/env python
"""Why was that crawl slow? One page of facts to compare between machines.

Prints the code version, the settings that decide crawl speed, what each
recent run actually did, where its first half hour went, how the model
endpoint behaved, and (on macOS) whether the machine slept mid-run.

    .venv/bin/python scripts/crawl_report.py [--log /tmp/agentscrape-crawls/api.log] [--hours 6]

Run it on both machines and diff the output: the usual answers are a
different CRAWL_STRATEGY, a model endpoint that is slower today, several
schools sharing one LLM_CONCURRENCY budget, or a laptop that went to sleep.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+")
_PHASES = (
    ("discovery finished", re.compile(r"pipeline\.discover\] discovery for")),
    ("programs planned", re.compile(r"pipeline\.plan\] .*programs planned")),
    ("html map finished", re.compile(r"pipeline\.html_map\] html map for")),
    ("first page read", re.compile(r"llm\.reader\] read ")),
)
_FAILURE = re.compile(r"model call failed \(([^)]+)\)")
_READ = re.compile(r"llm\.reader\] read ")


def stamp(line: str) -> datetime | None:
    match = _STAMP.match(line)
    return datetime.fromisoformat(match.group(1)) if match else None


def settings_table() -> list[tuple[str, object]]:
    from agentscrape.config import settings

    return [
        ("crawl_strategy", settings.crawl_strategy),
        ("llm_model / text", f"{settings.llm_model} / {settings.text_model}"),
        ("llm_concurrency (whole process)", settings.llm_concurrency),
        ("llm_timeout_seconds", settings.llm_timeout_seconds),
        ("llm_page_chunk_chars", settings.llm_page_chunk_chars),
        ("requests_per_second_per_domain", settings.requests_per_second_per_domain),
        ("in_site_fetch_concurrency", settings.in_site_fetch_concurrency),
        ("default_concurrency", settings.default_concurrency),
        ("default_step_budget", settings.default_step_budget),
        ("discovery_timeout_seconds", settings.discovery_timeout_seconds),
        ("link_rank_timeout_seconds", settings.link_rank_timeout_seconds),
        ("html_map_max_pages", settings.html_map_max_pages),
        ("site_timeout_seconds", settings.site_timeout_seconds),
        ("enable_crt_sh", settings.enable_crt_sh),
    ]


async def recent_runs(hours: int) -> list[dict]:
    from sqlalchemy import text

    from agentscrape.db.session import dispose_engine, get_sessionmaker

    query = text(
        """
        select r.id, r.status, r.stop_reason, r.config->>'crawl_strategy' as strategy,
               r.config->'modes' as modes, s.root_domain, sr.status as site_status,
               sr.error_code, sr.steps_taken, r.spend_usd, r.tokens_in, r.tokens_out,
               sr.started_at, coalesce(sr.finished_at, now()) as ended,
               (select count(*) from records x where x.site_id = s.id
                  and x.last_run_id = r.id) as people,
               (select count(*) from records x where x.site_id = s.id
                  and x.last_run_id = r.id and x.category in ('resident','fellow')) as trainees
        from runs r
        join site_runs sr on sr.run_id = r.id
        join sites s on s.id = sr.site_id
        where r.created_at > now() - make_interval(hours => :hours)
        order by sr.started_at
        """
    )
    async with get_sessionmaker()() as session:
        rows = (await session.execute(query, {"hours": hours})).mappings().all()
    await dispose_engine()
    return [dict(row) for row in rows]


def log_report(path: Path, hours: int) -> None:
    if not path.exists():
        print(f"\nno log at {path}; pass --log")
        return
    # The log and pmset both stamp local time without an offset, so the
    # comparisons here stay naive on purpose.
    cutoff = datetime.now() - timedelta(hours=hours)  # noqa: DTZ005
    lines = list(path.read_text(errors="replace").splitlines())
    recent = [line for line in lines if (s := stamp(line)) and s >= cutoff]
    if not recent:
        print(f"\nnothing in {path} from the last {hours}h")
        return

    print(f"\n== log ({path.name}, last {hours}h) ==")
    reads = [s for line in recent if _READ.search(line) and (s := stamp(line))]
    print(f"pages read by the model: {len(reads)}")
    if len(reads) > 1:
        span = (reads[-1] - reads[0]).total_seconds() / 60
        if span > 0:
            print(f"reading rate: {len(reads) / span:.1f} pages/min over {span:.0f} min")
    failures = Counter(
        m.group(1) for line in recent if (m := _FAILURE.search(line))
    )
    print(f"model call failures: {dict(failures) or 'none'}")

    print("\n== phases (first time each appears) ==")
    start = stamp(recent[0])
    for label, pattern in _PHASES:
        hit = next((s for line in recent if pattern.search(line) and (s := stamp(line))), None)
        if hit and start:
            print(f"  {label:22} {hit:%H:%M:%S}  (+{(hit - start).total_seconds() / 60:.1f} min)")
        else:
            print(f"  {label:22} never")


def sleep_report(hours: int) -> None:
    if sys.platform != "darwin":
        return
    try:
        out = subprocess.run(
            ["pmset", "-g", "log"], capture_output=True, text=True, timeout=60
        ).stdout
    except Exception:
        return
    cutoff = datetime.now() - timedelta(hours=hours)  # noqa: DTZ005
    sleeps = []
    for line in out.splitlines():
        if "Entering Sleep" not in line:
            continue
        try:
            when = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
        except ValueError:
            continue
        if when >= cutoff:
            sleeps.append(when)
    print(f"\n== machine sleep (last {hours}h) ==")
    print(
        f"  {len(sleeps)} sleep events"
        + (f", latest {sleeps[-1]:%H:%M:%S}" if sleeps else "")
        + ("  <- a sleeping machine stalls fetches and model calls" if sleeps else "")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", default="/tmp/agentscrape-crawls/api.log")
    parser.add_argument("--hours", type=int, default=6)
    args = parser.parse_args()

    version = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--oneline", "-1"], capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    print("== version ==")
    print(f"  commit: {version}")
    print(f"  uncommitted files: {len(dirty.splitlines())}")

    print("\n== settings that decide speed ==")
    for name, value in settings_table():
        print(f"  {name:34} {value}")

    import asyncio

    rows = asyncio.run(recent_runs(args.hours))
    print(f"\n== schools crawled in the last {args.hours}h ==")
    if not rows:
        print("  none")
    for row in rows:
        minutes = (row["ended"] - row["started_at"]).total_seconds() / 60 if row["started_at"] else 0
        print(
            f"  {row['root_domain'][:34]:34} {row['site_status'] or '?':9} "
            f"{row['error_code'] or '':16} strategy={row['strategy'] or 'default'} "
            f"modes={row['modes']}"
        )
        print(
            f"    {minutes:5.1f} min  pages={row['steps_taken']:<5} people={row['people']:<6}"
            f" residents/fellows={row['trainees']:<5} spend=${float(row['spend_usd'] or 0):.2f}"
            f"  tokens={row['tokens_in']:,}/{row['tokens_out']:,}"
        )

    log_report(Path(args.log), args.hours)
    sleep_report(args.hours)
    print(
        "\nNote: llm_concurrency is shared by every run in the process, so N schools at"
        "\nonce get roughly 1/N of the model throughput each."
    )


if __name__ == "__main__":
    main()
