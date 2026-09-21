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
    no_browser: bool = typer.Option(
        False, "--no-browser", help="HTML fetching only; never escalate to Chromium"
    ),
    budget: int = typer.Option(None, "--budget", help="Step budget for this site"),
    also: list[str] = typer.Option(
        None, "--also",
        help="Another registrable domain this institution publishes on, e.g. "
             "--also uchicagomedicine.org (repeatable)",
    ),
    strategy: str = typer.Option(
        None, "--strategy",
        help="hybrid (HTML pass first, model only where people show) or agent "
             "(model everywhere); default CRAWL_STRATEGY",
    ),
    mode: list[str] = typer.Option(
        None, "--mode",
        help="crawl and/or directory (repeatable); default crawl",
    ),
) -> None:
    """Run one site end to end."""
    if strategy not in (None, "hybrid", "agent"):
        raise typer.BadParameter("--strategy must be hybrid or agent")
    modes = list(dict.fromkeys(mode or ["crawl"]))
    if any(m not in ("crawl", "directory") for m in modes):
        raise typer.BadParameter("--mode must be crawl or directory")
    asyncio.run(
        _run_site(
            url, dry_run=dry_run, no_browser=no_browser,
            budget=budget, also=list(also or []),
            strategy=strategy, modes=modes,
        )
    )


async def _run_site(
    url: str, *, dry_run: bool, no_browser: bool,
    budget: int | None, also: list[str] | None = None,
    strategy: str | None = None, modes: list[str] | None = None,
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
            config={"source": "cli", "concurrency": 1, "crawl_strategy": strategy or settings.crawl_strategy,
                    "modes": modes or ["crawl"]}, sites_total=1,
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    site_id, site_run_id, root_url = await ensure_site_run(
        run_id=run_id, site_url=url, step_budget=budget
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
            run_id=run_id, step_budget=budget,
            allowed_domains=also, browser_context=context,
            crawl_strategy=strategy, modes=modes,
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
        "status", "steps_taken",
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


@app.command("export-school")
def export_school_command(
    domain: str = typer.Argument(..., help="The school's root domain, as stored"),
    path: str = typer.Argument(..., help="Where to write the JSON snapshot"),
) -> None:
    """Write one school's people and provenance to a JSON snapshot."""
    from pathlib import Path

    from .db.session import dispose_engine
    from .snapshot import export_school

    async def _go() -> None:
        count = await export_school(domain, Path(path))
        await dispose_engine()
        console.print(f"[green]Exported[/green] {count} people from {domain} to {path}")

    asyncio.run(_go())


@app.command("import-school")
def import_school_command(
    path: str = typer.Argument(..., help="A snapshot written by export-school"),
    replace: bool = typer.Option(False, "--replace", help="Delete the school first"),
) -> None:
    """Load a school snapshot, so a fresh database opens on real results."""
    from pathlib import Path

    from .db.session import dispose_engine
    from .snapshot import import_school

    async def _go() -> None:
        count = await import_school(Path(path), replace=replace)
        await dispose_engine()
        console.print(
            f"[green]Imported[/green] {count} people from {path}"
            if count else f"{path}: school already has people; nothing imported"
        )

    asyncio.run(_go())


@app.command("load-schools")
def load_schools_command(
    folder: str = typer.Argument(None, help="Folder of school sheets (default: schools/)"),
) -> None:
    """Load the school sheets in schools/ into the school list. Idempotent."""
    from pathlib import Path

    from .db.session import dispose_engine, session_scope
    from .schools_sheet import load_school_sheets, sheets_dir

    async def _go() -> None:
        target = Path(folder) if folder else sheets_dir()
        async with session_scope() as session:
            result = await load_school_sheets(session, target)
        console.print(
            f"[green]Loaded[/green] {target}: {result.created} created, "
            f"{result.updated} updated, {result.unchanged} unchanged, "
            f"{result.merged_rows} duplicate rows merged, {result.failed_rows} failed, "
            f"{result.skipped_files} unreadable files."
        )
        await dispose_engine()

    asyncio.run(_go())


@app.command("import-school-catalog")
def import_school_catalog(
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and report without writing"),
) -> None:
    """Make the 23 schools in data/schools-23.csv the active list; nothing is deleted."""
    from .db.session import dispose_engine, get_sessionmaker
    from .school_catalog import import_catalog

    async def _go() -> None:
        async with get_sessionmaker()() as session:
            result = await import_catalog(session, dry_run=dry_run)
        await dispose_engine()
        console.print(result)

    asyncio.run(_go())


@app.command()
def sweep() -> None:
    """Expire screenshots and exports past their retention window."""
    from .storage.artifacts import sweep_expired_exports, sweep_expired_screenshots

    async def _go() -> None:
        console.print("screenshots:", await sweep_expired_screenshots())
        console.print("exports:", await sweep_expired_exports())

    asyncio.run(_go())


@app.command("llm-check")
def llm_check() -> None:
    """Confirm the model endpoint answers text, image and JSON requests.

    Run this before any crawl: the pipeline now depends on the model for every
    page, and a misconfigured endpoint aborts sites rather than degrading.
    """
    from .llm.provider import get_provider

    async def _go() -> bool:
        provider = get_provider()
        ok = True
        checks = [
            ("text", settings.text_model, None,
             'Return ONLY this JSON: {"people": [{"full_name": "Ada Lovelace"}]}'),
            ("image", settings.llm_model, _solid_png(64, 32, (220, 20, 20)),
             'What colour is the attached image? Return ONLY JSON: {"colour": "<name>"}'),
        ]
        if settings.cheap_model != settings.text_model:
            checks.append(
                ("triage", settings.cheap_model, None,
                 'Return ONLY this JSON: {"links": [{"i": 0, "p": 80}]}')
            )
        for label, model, image, prompt in checks:
            try:
                response = await provider.complete(
                    system="You answer with strict JSON only.", user=prompt,
                    image_bytes=image, model=model, max_tokens=200,
                )
            except Exception as exc:  # report every failure mode plainly
                console.print(f"[red]FAIL[/red] {label} ({model}): {type(exc).__name__}: {exc}")
                ok = False
                continue
            parsed = response.json()
            status = "[green]ok[/green]" if isinstance(parsed, dict) else "[red]FAIL (not JSON)[/red]"
            ok = ok and isinstance(parsed, dict)
            console.print(
                f"{status} {label} ({model}): {response.text.strip()[:200]!r} "
                f"[dim]{response.usage.input_tokens} in / {response.usage.output_tokens} out[/dim]"
            )
        return ok

    console.print(f"endpoint {settings.llm_base_url}")
    if not asyncio.run(_go()):
        raise typer.Exit(1)


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A tiny solid-colour PNG, so the image check needs no fixture file."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


@app.command()
def queue(
    sites: bool = typer.Option(
        False, "--sites", help="List every school inside each run"
    ),
) -> None:
    """Show the run queue: what holds the model budget, and what is next.

    Reads the database, so it reports the queue of the API process on this
    machine. A run only shows as running while that process has it in flight.
    """
    from .db.session import dispose_engine, get_sessionmaker
    from .orchestrator.scheduler import queue_view

    async def _go() -> None:
        async with get_sessionmaker()() as session:
            view = await queue_view(session)
        await dispose_engine()

        if not (view.running or view.waiting or view.stalled):
            console.print("[dim]Nothing running and nothing queued.[/dim]")
            return

        table = Table(title="run queue")
        table.add_column("#", style="bold", justify="right")
        table.add_column("run")
        table.add_column("state")
        table.add_column("schools", justify="right")
        table.add_column("people", justify="right")
        table.add_column("spend", justify="right")

        groups = (
            ("[green]running[/green]", view.running),
            ("[yellow]waiting[/yellow]", view.waiting),
            ("[red]stalled[/red]", view.stalled),
        )
        for state, entries in groups:
            for entry in entries:
                table.add_row(
                    str(entry.position) if entry.position else "-",
                    entry.label or entry.run_id[:18],
                    state,
                    f"{entry.sites_completed}/{entry.sites_total}",
                    f"{entry.records_found:,}",
                    f"${entry.spend_usd:.2f}",
                )
        console.print(table)

        if sites:
            for state, entries in groups:
                for entry in entries:
                    console.print(f"\n{entry.label or entry.run_id} ({state}):")
                    for site in entry.sites:
                        console.print(
                            f"  {site.domain or site.site_id:38} {site.status:10}"
                            f" pages={site.steps_taken:<6} people={site.records_found:,}"
                        )

        if view.waiting and not view.running:
            console.print(
                "\n[dim]Nothing is running, so the next queued run starts when the "
                "API process is up.[/dim]"
            )

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
