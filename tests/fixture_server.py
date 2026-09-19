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

NEWS = """
<html><head><title>News</title></head><body><h1>A new wing opens</h1>
<p>The hospital opened its new east wing this week after two years of
construction. The building adds forty inpatient beds, a rooftop garden and a
larger cafeteria, and it connects to the parking garage by a covered walkway.</p>
<p>Visiting hours in the new wing match the rest of the hospital. Parking
validation is available at the main information desk on the ground floor, and
the gift shop has moved next to the new entrance.</p>
</body></html>
"""

# The people directory. A site-wide search in the header must not be taken
# for it; the directory's own form is the one that finds people.
DIRECTORY = """
<html><head><title>People Directory</title></head><body>
<header><form action="/search" method="get"><input name="s" placeholder="Search this site"></form></header>
<h1>People Directory</h1>
<form action="/directory/search" method="get" id="people-search">
  <input type="hidden" name="scope" value="all">
  <label for="who">Name</label><input id="who" name="q" type="text">
  <button type="submit">Search</button>
</form>
</body></html>
"""

# name -> result rows (name, email or None, detail line, profile slug or None)
DIRECTORY_ENTRIES = {
    "ann riley": [("Ann Riley", "ann.riley@example.edu", "Resident, Class of 2027", None)],
    "cara diaz": [("Cara Diaz", None, "Resident", "cara-diaz")],
    "ben cole": [
        ("Ben Cole", "ben.cole@example.edu", "Resident", None),
        ("Ben Cole", "bcole2@example.edu", "Staff", None),
    ],
}

PROFILES = {
    "cara-diaz": """
<html><head><title>Cara Diaz | People</title></head><body>
<h1>Cara Diaz</h1><p>Resident, Internal Medicine, PGY-3</p>
<p><a href="mailto:cara.diaz@example.edu">cara.diaz@example.edu</a></p>
</body></html>
""",
}


def directory_results(query: str) -> str:
    # Like most search pages, the query is echoed back whether or not anyone
    # matched it, so a name on the page is not by itself a result.
    from html import escape

    echo = f"<h1>Search for {escape(query)}</h1>"
    rows = DIRECTORY_ENTRIES.get(" ".join(query.lower().split()), [])
    if not rows:
        return f"<html><head><title>Search</title></head><body>{echo}<p>No results found.</p></body></html>"
    items = []
    for name, email, detail, slug in rows:
        who = f'<a href="/directory/people/{slug}">{name}</a>' if slug else name
        mail = f'<a href="mailto:{email}">{email}</a>' if email else ""
        items.append(f'<div class="person-card"><h3>{who}</h3><p>{detail}</p>{mail}</div>')
    return f"<html><head><title>Search results</title></head><body>{echo}{''.join(items)}</body></html>"


PRIVATE_DIRECTORY = """
<html><head><title>Sign in</title></head><body>
<form action="/login" method="post"><input name="user"><input type="password" name="pw"></form>
</body></html>
"""

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

    async def directory(request):
        return HTMLResponse(DIRECTORY)

    async def directory_search(request):
        return HTMLResponse(directory_results(request.query_params.get("q", "")))

    async def site_search(request):
        return HTMLResponse("<html><body><p>No pages match.</p></body></html>")

    async def profile(request):
        body = PROFILES.get(request.path_params["slug"])
        return HTMLResponse(body or "<html><body>Not found</body></html>", status_code=200 if body else 404)

    async def private_directory(request):
        return HTMLResponse(PRIVATE_DIRECTORY)

    return Starlette(
        routes=[
            Route("/", home),
            Route("/sitemap.xml", sitemap),
            Route("/robots.txt", robots),
            Route("/residents", residents),
            Route("/fellows", fellows),
            Route("/alumni", alumni),
            Route("/news", news),
            Route("/directory", directory),
            Route("/directory/search", directory_search),
            Route("/directory/people/{slug}", profile),
            Route("/search", site_search),
            Route("/private-directory", private_directory),
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
