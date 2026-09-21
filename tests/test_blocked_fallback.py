"""A school that turns scripts away but lets a browser in is still crawled.

The site here answers 403 to any request without the headers a browser sends
(Chromium sets `sec-fetch-mode`; a plain HTTP client does not), which is the
shape of what several hospital sites do. Before, the crawl gave up on the first
refusal without ever opening the page the way a person would.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

from agentscrape.config import settings
from agentscrape.db.enums import RunStatus, SiteRunStatus
from agentscrape.db.models import Record, Run, SiteRun
from agentscrape.domain.schemas import RunConfigIn, RunCreate
from agentscrape.orchestrator.limits import RunLimits
from agentscrape.orchestrator.pool import RunOrchestrator
from agentscrape.orchestrator.service import create_run

ROSTER = """<html><head><title>Internal Medicine Residency - Current Residents</title></head>
<body><h1>Current Residents</h1>
<p>Our graduate medical education program trains residents at this academic medical center.</p>
<table><tr><th>Name</th><th>PGY</th><th>Email</th></tr>
<tr><td>Ann Riley, MD</td><td>PGY-1</td><td><a href="mailto:ann.riley@example.edu">ann.riley@example.edu</a></td></tr>
<tr><td>Ben Okafor, MD</td><td>PGY-2</td><td><a href="mailto:ben.okafor@example.edu">ben.okafor@example.edu</a></td></tr>
<tr><td>Cara Diaz, MD</td><td>PGY-3</td><td><a href="mailto:cara.diaz@example.edu">cara.diaz@example.edu</a></td></tr>
</table></body></html>"""


def _app(refuse_scripts: bool) -> Starlette:
    async def page(request):
        if refuse_scripts and "sec-fetch-mode" not in request.headers:
            return HTMLResponse("<h1>Forbidden</h1>", status_code=403)
        return HTMLResponse(ROSTER)

    return Starlette(routes=[Route("/{path:path}", page), Route("/", page)])


@contextmanager
def _serve(hostname: str, refuse_scripts: bool = True):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_app(refuse_scripts), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://{hostname}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.setattr(settings, "enable_crt_sh", False)
    monkeypatch.setattr(settings, "requests_per_second_per_domain", 50.0)
    monkeypatch.setattr(settings, "discovery_timeout_seconds", 30)
    monkeypatch.setattr(settings, "discovery_source_timeout_seconds", 10)
    from agentscrape.browser import ratelimit

    monkeypatch.setattr(ratelimit, "_limiter", None)


async def _crawl(session, entry: str, *, use_browser: bool) -> str:
    run = await create_run(
        session,
        RunCreate(sites=[entry], config=RunConfigIn(concurrency=1, step_budget=10, modes=["crawl"])),
    )
    await RunOrchestrator(
        run.id, concurrency=1, skip_threshold=0.9, step_budget=10, limits=RunLimits(),
        use_browser=use_browser, crawl_strategy="agent", modes=["crawl"],
    ).start()
    return run.id


@pytest.mark.asyncio
async def test_a_site_that_refuses_scripts_is_read_in_a_browser(session) -> None:
    with _serve("guarded.localhost") as base:
        run_id = await _crawl(session, f"{base}/residents", use_browser=True)

    session.expire_all()
    run = await session.get(Run, run_id)
    site_run = (await session.execute(select(SiteRun).where(SiteRun.run_id == run_id))).scalar_one()
    names = {r.full_name for r in (await session.execute(select(Record))).scalars()}

    assert run.status == RunStatus.COMPLETED
    assert site_run.status == SiteRunStatus.COMPLETED, site_run.error_message
    assert {"Ann Riley", "Ben Okafor", "Cara Diaz"} <= names


@pytest.mark.asyncio
async def test_without_a_browser_the_refusal_is_reported_with_its_reason(session) -> None:
    with _serve("guarded2.localhost") as base:
        run_id = await _crawl(session, f"{base}/residents", use_browser=False)

    site_run = (await session.execute(select(SiteRun).where(SiteRun.run_id == run_id))).scalar_one()
    assert site_run.status == SiteRunStatus.REJECTED
    assert site_run.error_code == "SITE_BLOCKED"
    assert "403" in (site_run.error_message or "")


@pytest.mark.asyncio
async def test_a_site_that_answers_everyone_is_unaffected(session) -> None:
    with _serve("open.localhost", refuse_scripts=False) as base:
        run_id = await _crawl(session, f"{base}/residents", use_browser=False)

    site_run = (await session.execute(select(SiteRun).where(SiteRun.run_id == run_id))).scalar_one()
    assert site_run.status == SiteRunStatus.COMPLETED


EMPTY = """<html><head><title>Residency Programs</title></head><body>
<h1>Residency Programs</h1>
<p>Our graduate medical education office supports residents and fellows across many programs at this academic medical center.</p>
<p>Contact the program coordinator for more information about each residency.</p></body></html>"""


@pytest.mark.asyncio
async def test_a_school_that_finishes_with_nobody_says_why(session, monkeypatch) -> None:
    async def page(request):
        return HTMLResponse(EMPTY)

    app = Starlette(routes=[Route("/{path:path}", page), Route("/", page)])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        run_id = await _crawl(session, f"http://quiet.localhost:{port}/residents", use_browser=False)
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    site_run = (await session.execute(select(SiteRun).where(SiteRun.run_id == run_id))).scalar_one()
    assert site_run.status == SiteRunStatus.COMPLETED
    assert site_run.error_code in ("NO_PEOPLE_FOUND", "NO_PAGES_TO_READ")
    assert "no residents or fellows" in site_run.error_message or "No page" in site_run.error_message


def test_the_coverage_summary_counts_programs_with_a_roster() -> None:
    from agentscrape.llm.planner import FOUND, PENDING
    from agentscrape.pipeline.nodes.finalize import _coverage

    coverage = _coverage({
        "steps_taken": 12,
        "programs": [
            {"name": "Internal Medicine", "kind": "residency", "status": FOUND, "people": 30},
            {"name": "Cardiology", "kind": "fellowship", "status": PENDING},
        ],
    })
    assert (coverage["programs_total"], coverage["programs_covered"], coverage["pages_read"]) == (2, 1, 12)
    assert coverage["programs"][0]["people"] == 30
    assert _coverage({"programs": []}) is None
