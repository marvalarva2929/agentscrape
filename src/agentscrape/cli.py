"""Command line entry points.

`agentscrape site <url>` runs the whole per-site pipeline with no concurrency,
which is the fastest way to confirm the pipeline is correct before the
orchestrator parallelizes it.
"""

from __future__ import annotations

import asyncio
import logging

import typer
from rich.console import Console
from rich.table import Table

from .config import settings

app = typer.Typer(add_completion=False, help="agentscrape backend operations")
console = Console()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
)


@app.command()
def site(
    url: str = typer.Argument(..., help="Root URL or domain of the institution"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Discover and rank candidates, then stop"
    ),
    force: bool = typer.Option(False, "--force", help="Ignore the skip check"),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="HTML fetching only; never escalate to Chromium"
    ),
    budget: int = typer.Option(None, "--budget", help="Step budget for this site"),
    also: list[str] = typer.Option(
        None, "--also",
        help="Another registrable domain this institution publishes on, e.g. "
             "--also uchicagomedicine.org (repeatable)",
    ),
    threshold: float = typer.Option(None, "--threshold", help="Skip similarity threshold"),
) -> None:
    """Run one site end to end."""
    asyncio.run(
        _run_site(
            url, dry_run=dry_run, force=force, no_browser=no_browser,
            budget=budget, threshold=threshold, also=list(also or []),
        )
    )


async def _run_site(
    url: str, *, dry_run: bool, force: bool, no_browser: bool,
    budget: int | None, threshold: float | None, also: list[str] | None = None,
) -> None:
    from .browser.renderer import BrowserPool
    from .db.enums import RunStatus
    from .db.models import Run
    from .db.session import dispose_engine, get_sessionmaker
    from .pipeline.runner import ensure_site_run, run_site

    settings.ensure_dirs()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    async with get_sessionmaker()() as session:
        run = Run(
            status=RunStatus.RUNNING, label=f"cli:{url}",
            config={"source": "cli", "concurrency": 1}, sites_total=1,
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    site_id, site_run_id, root_url = await ensure_site_run(
        run_id=run_id, site_url=url, force_rescan=force, step_budget=budget
    )
    console.print(f"[bold]site[/bold] {root_url}  [dim]run={run_id} site_run={site_run_id}[/dim]")

    if dry_run:
        await _dry_run(root_url, site_id)
        await dispose_engine()
        return

    pool = None
    context = None
    if not no_browser:
        pool = BrowserPool(size=1)
        await pool.start()
        context = await pool.acquire("agent-0")

    try:
        state = await run_site(
            site_id=site_id, site_run_id=site_run_id, root_url=root_url,
            run_id=run_id, force_rescan=force, step_budget=budget,
            skip_threshold=threshold, allowed_domains=also, browser_context=context,
        )
    finally:
        if pool is not None:
            await pool.stop()

    _print_outcome(state)
    await _print_records(site_id)

    async with get_sessionmaker()() as session:
        db_run = await session.get(Run, run_id)
        if db_run is not None:
            db_run.status = RunStatus.COMPLETED
            db_run.records_found = (
                state.get("records_new", 0) + state.get("records_changed", 0)
                + state.get("records_unchanged", 0)
            )
            db_run.records_new = state.get("records_new", 0)
            db_run.records_changed = state.get("records_changed", 0)
        await session.commit()

    await dispose_engine()


async def _dry_run(root_url: str, site_id: str) -> None:
    """Show the ranked candidate list without visiting anything."""
    from .browser.fetcher import Fetcher
    from .db.session import get_sessionmaker
    from .pipeline.deps import PipelineDeps
    from .pipeline.nodes.discover import discover_links
    from .pipeline.state import initial_state
    from .urls import host_of

    async with Fetcher() as fetcher:
        deps = PipelineDeps(fetcher=fetcher, sessionmaker=get_sessionmaker())
        state = initial_state(
            site_id=site_id, site_run_id="dry-run", root_url=root_url,
            root_domain=host_of(root_url),
        )
        state = await discover_links(state, deps)

    table = Table(title=f"candidates ({len(state['candidates'])} kept of "
                        f"{state['candidates_considered']} discovered)")
    table.add_column("score", justify="right", style="cyan")
    table.add_column("known", justify="center")
    table.add_column("url", overflow="fold")
    for candidate in state["candidates"][:40]:
        table.add_row(
            f"{candidate['score']:.1f}",
            "*" if candidate["is_known_path"] else "",
            candidate["url"],
        )
    console.print(table)


def _print_outcome(state) -> None:
    table = Table(title="site run", show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value")
    for key in (
        "status", "skip_reason", "similarity_score", "steps_taken",
        "candidates_considered", "known_path_hits", "records_new",
        "records_changed", "records_unchanged", "records_missing",
        "error_code", "error_message",
    ):
        value = state.get(key)
        if value not in (None, "", 0) or key in ("status", "records_new"):
            table.add_row(key, str(value))
    table.add_row("candidates", str(len(state.get("candidates", []))))
    console.print(table)


async def _print_records(site_id: str, limit: int = 30) -> None:
    from sqlalchemy import select

    from .db.models import Record, RecordVersion
    from .db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Record).where(Record.site_id == site_id)
                .order_by(Record.confidence.desc()).limit(limit)
            )
        ).scalars().all()
        total = len(
            (await session.execute(select(Record.id).where(Record.site_id == site_id)))
            .scalars().all()
        )
        shots = (
            await session.execute(
                select(RecordVersion.screenshot_available)
                .join(Record, Record.current_version_id == RecordVersion.id)
                .where(Record.site_id == site_id)
            )
        ).scalars().all()

    if not rows:
        console.print("[yellow]no records stored for this site[/yellow]")
        return

    table = Table(title=f"records ({total} total, {sum(1 for s in shots if s)} with screenshots)")
    for column in ("name", "email", "category", "position", "PGY", "class", "specialty", "status"):
        table.add_column(column, overflow="fold")
    for record in rows:
        table.add_row(
            record.full_name or "-", record.email or "-", record.category,
            (record.position or "-")[:28],
            str(record.pgy_at_capture or "-"), str(record.class_of or "-"),
            record.specialty_normalized or "-", record.status,
        )
    console.print(table)


@app.command("seed-demo")
def seed_demo_command(
    reset: bool = typer.Option(
        False, "--reset", help="Clear existing data first"
    ),
) -> None:
    """Populate demo data so the app can be explored without crawling."""
    from .demo import seed_demo

    async def _go() -> None:
        counts = await seed_demo(reset=reset)
        console.print(
            f"[green]Seeded[/green] {counts['schools']} schools, "
            f"{counts['programs']} programmes, {counts['people']} people."
        )

    asyncio.run(_go())


@app.command()
def sweep() -> None:
    """Expire screenshots and exports past their retention window."""
    from .storage.artifacts import sweep_expired_exports, sweep_expired_screenshots

    async def _go() -> None:
        console.print("screenshots:", await sweep_expired_screenshots())
        console.print("exports:", await sweep_expired_exports())

    asyncio.run(_go())


@app.command()
def config() -> None:
    """Print effective configuration."""
    table = Table(title="settings")
    table.add_column("key", style="bold")
    table.add_column("value", overflow="fold")
    for key, value in settings.model_dump().items():
        if "password" in key or "api_key" in key:
            value = "***" if value else "(unset)"
        table.add_row(key, str(value))
    console.print(table)


if __name__ == "__main__":
    app()
