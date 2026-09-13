"""A tiny institution website, served locally.

Lets the orchestrator be exercised end to end — discovery, extraction,
reconciliation, known paths, skip, hard stops — with no network dependency and
no wait on a real university's rate limits. Content is mutable so a second run
can be made to see a changed roster.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

# Homepage text carries post-secondary signals so the K-12 gate accepts it.
HOME = """
<html><head><title>Example Teaching Hospital</title></head><body>
<h1>Example Teaching Hospital</h1>
<p>Our internal medicine residency program and graduate medical education
office support more than 200 trainees at this academic medical center.</p>
<a href="/residents">Current Residents</a>
<a href="/fellows">Our Fellows</a>
<a href="/alumni">Alumni</a>
<a href="/news">News</a>
</body></html>
"""

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/</loc></url>
  <url><loc>{base}/residents</loc></url>
  <url><loc>{base}/fellows</loc></url>
  <url><loc>{base}/alumni</loc></url>
  <url><loc>{base}/news</loc></url>
</urlset>
"""


def residents_page(people: list[tuple[str, int, str]]) -> str:
    rows = "".join(
        f"<tr><td>{name}, MD</td><td>PGY-{pgy}</td>"
        f'<td><a href="mailto:{email}">{email}</a></td></tr>'
        for name, pgy, email in people
    )
    return f"""
<html><head><title>Internal Medicine Residency - Current Residents</title></head>
<body><h1>Current Residents</h1>
<table><tr><th>Name</th><th>PGY</th><th>Email</th></tr>{rows}</table>
</body></html>
"""


FELLOWS = """
<html><head><title>Cardiology Fellows</title></head><body>
<div class="fellow-card"><h3>Dana Fields, MD</h3><p>Fellow, Class of 2027</p>
  <a href="mailto:dana.fields@example.edu">dana.fields@example.edu</a></div>
<div class="staff-card"><h3>Owen Grant, MD</h3><p>Program Director</p>
  <a href="mailto:owen.grant@example.edu">owen.grant@example.edu</a></div>
</body></html>
"""

# Out of scope: former trainees.
ALUMNI = """
<html><head><title>Alumni | Example Teaching Hospital</title></head><body>
<table><tr><th>Name</th><th>Email</th></tr>
<tr><td>Gone Person, MD</td><td><a href="mailto:gone@example.edu">gone@example.edu</a></td></tr>
</table></body></html>
"""

NEWS = "<html><head><title>News</title></head><body><p>A new wing opened.</p></body></html>"

DEFAULT_RESIDENTS = [
    ("Ann Riley", 1, "ann.riley@example.edu"),
    ("Ben Cole", 2, "ben.cole@example.edu"),
    ("Cara Diaz", 3, "cara.diaz@example.edu"),
]


class FixtureSite:
    """Mutable state so tests can change the roster between runs."""

    def __init__(self) -> None:
        self.residents = list(DEFAULT_RESIDENTS)
        self.base = ""


def build_app(state: FixtureSite) -> Starlette:
    async def home(request):
        return HTMLResponse(HOME)

    async def sitemap(request):
        return PlainTextResponse(
            SITEMAP.format(base=state.base), media_type="application/xml"
        )

    async def robots(request):
        return PlainTextResponse(f"User-agent: *\nSitemap: {state.base}/sitemap.xml\n")

    async def residents(request):
        return HTMLResponse(residents_page(state.residents))

    async def fellows(request):
        return HTMLResponse(FELLOWS)

    async def alumni(request):
        return HTMLResponse(ALUMNI)

    async def news(request):
        return HTMLResponse(NEWS)

    return Starlette(
        routes=[
            Route("/", home),
            Route("/sitemap.xml", sitemap),
            Route("/robots.txt", robots),
            Route("/residents", residents),
            Route("/fellows", fellows),
            Route("/alumni", alumni),
            Route("/news", news),
        ]
    )


@contextmanager
def serve(hostname: str = "site.localhost"):
    """Run the fixture site on an ephemeral port for the duration of a test.

    The server binds 127.0.0.1 but is addressed by a `*.localhost` name, which
    resolves to loopback and contains a dot. Sites are keyed by hostname, so two
    fixtures on 127.0.0.1 would otherwise be one institution, and a dotless
    hostname is rejected as an invalid site input.
    """
    import socket

    state = FixtureSite()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    state.base = f"http://{hostname}:{port}"

    config = uvicorn.Config(
        build_app(state), host="127.0.0.1", port=port, log_level="error"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import time

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    try:
        yield state
    finally:
        server.should_exit = True
        thread.join(timeout=5)
