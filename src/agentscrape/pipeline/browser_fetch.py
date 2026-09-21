"""Read a page in the browser when plain HTTP is turned away.

Many hospital and university sites refuse a request that is not a browser: a
403 for a script, the same page for Chromium. Giving up on the first refusal
left those schools reading "blocked" without ever having tried to open the page
the way a person would. This opens it in the site's browser context and hands
back the same `FetchResult` the rest of the pipeline already understands.

It is not a way around a site that says no. A 429 is an explicit request to
slow down and is never retried here, and a page that turns out to be an
access-denied or bot-protection page is reported as blocked, not read.
"""

from __future__ import annotations

import asyncio

from ..browser.fetcher import FetchResult
from ..browser.renderer import render_page
from ..config import settings
from .deps import PipelineDeps


async def browser_fetch(deps: PipelineDeps, url: str) -> FetchResult:
    if deps.browser_context is None:
        return FetchResult(url=url, final_url=url, status=None, text="", content_type="",
                           ok=False, error="no browser available")
    try:
        rendered = await asyncio.wait_for(
            render_page(deps.browser_context, url, capture_screenshot=False),
            timeout=settings.page_timeout_seconds + 15,
        )
    except TimeoutError:
        return FetchResult(url=url, final_url=url, status=None, text="", content_type="",
                           ok=False, error="the browser timed out")
    if not rendered.ok:
        return FetchResult(url=url, final_url=rendered.final_url, status=rendered.status,
                           text="", content_type="", ok=False, error=rendered.error)
    return FetchResult(
        url=url, final_url=rendered.final_url, status=rendered.status or 200, text=rendered.html,
        content_type="text/html", ok=True,
    )
