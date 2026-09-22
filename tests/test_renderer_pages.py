"""What the browser makes of pages that are not simply a page of text.

A refusal, a bot-protection interstitial and an embedded roster each used to be
read as an ordinary page: the first two as a school with nobody listed, the
third as an empty page. The crawler reports why instead of finding nothing.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import pytest
import pytest_asyncio
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

from agentscrape.browser.renderer import BrowserPool, looks_like_challenge, render_page

ROSTER = "<html><body><h1>Residents</h1>" + "".join(
    f"<div class='card'><h3>Resident {n} Example, MD</h3><p>PGY-{n % 3 + 1}</p></div>" for n in range(12)
) + "</body></html>"


def _pages() -> Starlette:
    async def parent(request):
        return HTMLResponse(
            "<html><head><title>Residents</title></head><body><h1>Meet our residents</h1>"
            "<iframe src='/roster'></iframe></body></html>"
        )

    async def roster(request):
        return HTMLResponse(ROSTER)

    async def forbidden(request):
        return HTMLResponse("<html><body><h1>Forbidden</h1></body></html>", status_code=403)

    async def challenge(request):
        return HTMLResponse(
            "<html><head><title>Just a moment...</title></head>"
            "<body><p>Checking your browser before accessing the site.</p></body></html>"
        )

    async def ordinary(request):
        return HTMLResponse("<html><head><title>Program</title></head><body>" + "<p>Our residency program. </p>" * 30 + "</body></html>")

    return Starlette(routes=[
        Route("/parent", parent), Route("/roster", roster), Route("/forbidden", forbidden),
        Route("/challenge", challenge), Route("/ordinary", ordinary),
    ])


@contextmanager
def _serve():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_pages(), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest_asyncio.fixture
async def context():
    pool = BrowserPool(size=1)
    await pool.start()
    try:
        yield await pool.acquire("test")
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_a_refusal_is_reported_as_a_refusal(context) -> None:
    with _serve() as base:
        result = await render_page(context, f"{base}/forbidden")
    assert result.ok is False
    assert result.status == 403 and "403" in (result.error or "")


@pytest.mark.asyncio
async def test_a_bot_protection_page_is_not_read_as_an_empty_school(context) -> None:
    with _serve() as base:
        result = await render_page(context, f"{base}/challenge")
    assert result.ok is False
    assert "bot-protection" in (result.error or "")


@pytest.mark.asyncio
async def test_a_roster_inside_an_iframe_is_part_of_the_page(context) -> None:
    with _serve() as base:
        result = await render_page(context, f"{base}/parent")
    assert result.ok
    assert "Resident 3 Example" in result.text
    assert "Resident 3 Example" in result.html


@pytest.mark.asyncio
async def test_an_ordinary_page_is_unchanged(context) -> None:
    with _serve() as base:
        result = await render_page(context, f"{base}/ordinary")
    assert result.ok and "residency program" in result.text


def test_a_long_page_that_mentions_access_denied_is_still_content() -> None:
    article = "Our policy on badge access denied after hours. " + "Lorem ipsum dolor sit amet. " * 300
    assert looks_like_challenge("Policies", article) == ""
    assert looks_like_challenge("Access Denied", "You do not have permission.") != ""
