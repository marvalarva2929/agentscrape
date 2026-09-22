"""Playwright rendering — the expensive path, used only on escalation.

Interaction targets come from the **accessibility tree** (role + name), never
from pixel coordinates: clicks and taps are driven by real DOM elements, not
guessed positions.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from ..config import settings
from ..urls import host_of
from .ratelimit import get_rate_limiter

log = logging.getLogger("agentscrape.render")

# Visible controls are collected broadly. The model, not a label regex, decides
# which one is useful for reaching current trainee information.
_CONTROLS_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(
    'button,[role=button],[role=tab],a:not([href]),[role=link]:not([href])'
  )) {
    const label = (el.innerText || el.getAttribute('aria-label') || el.title || '').trim();
    if (!label || label.length > 120) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : 'button');
    const key = `${role}:${label.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      role,
      name: label,
      href: el.getAttribute('href') || null,
      disabled: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
    });
  }
  return out.slice(0, 100);
}
"""

_LINKS_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('a[href]')) {
    const url = el.href;
    if (!url || !/^https?:/i.test(url) || seen.has(url)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const text = (el.innerText || el.getAttribute('aria-label') || el.title || '').trim();
    if (!text) continue;
    const container = el.closest('li,p,nav,section,article,div');
    const context = ((container && container.innerText) || text).replace(/\\s+/g, ' ').trim();
    seen.add(url);
    out.push({url, text: text.slice(0, 160), context: context.slice(0, 280)});
  }
  return out.slice(0, 300);
}
"""



# Cheap signal for "the roster has finished painting": count the things we
# actually extract, not arbitrary DOM size.
_CONTENT_SIGNAL_JS = """
() => {
  const mailtos = document.querySelectorAll('a[href^="mailto:"]').length;
  const headings = document.querySelectorAll('h1,h2,h3,h4,strong,[class*="name" i]').length;
  const text = document.body ? document.body.innerText.length : 0;
  return {mailtos, headings, text};
}
"""

SETTLE_POLL_SECONDS = 0.8
SETTLE_MAX_POLLS = 8
SETTLE_MIN_STABLE = 2
SCROLL_STEPS = 12

# Roster pages commonly lazy-load cards as they scroll into view, so the page
# must be walked to the bottom before its content can be read.
_AUTOSCROLL_JS = """
(steps) => new Promise((resolve) => {
  let i = 0;
  const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  const step = Math.max(Math.ceil(height / steps), 400);
  const timer = setInterval(() => {
    window.scrollBy(0, step);
    i += 1;
    const atBottom = (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 2;
    if (i >= steps || atBottom) { clearInterval(timer); window.scrollTo(0, 0); resolve(true); }
  }, 120);
})
"""


async def _wait_for_content(page: Page) -> None:
    """Scroll the page, then poll until extractable content stops growing.

    Two failure modes made this necessary, both of which look like a *successful*
    extraction of a handful of people rather than an error:

      * `networkidle` fires while a client-rendered roster is still painting.
      * Cards are lazy-loaded on scroll, so a page read at the top never sees
        most of the list.

    Stability is required across consecutive polls, and the counter is the
    number of things we actually extract rather than raw DOM size.
    """
    try:
        await page.evaluate(_AUTOSCROLL_JS, SCROLL_STEPS)
    except PlaywrightError:
        pass

    previous: tuple[int, int, int] | None = None
    stable = 0
    for _ in range(SETTLE_MAX_POLLS):
        try:
            signal = await page.evaluate(_CONTENT_SIGNAL_JS)
        except PlaywrightError:
            return
        current = (
            int(signal.get("mailtos", 0)),
            int(signal.get("headings", 0)),
            int(signal.get("text", 0)),
        )
        if current == previous and (current[0] or current[1]):
            stable += 1
            if stable >= SETTLE_MIN_STABLE:
                return
        else:
            stable = 0
        previous = current
        await asyncio.sleep(SETTLE_POLL_SECONDS)


@dataclass
class RenderResult:
    url: str
    final_url: str
    title: str
    html: str
    text: str
    controls: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    status: int | None = None
    # URLs of the GET requests the page made (search_in_browser only).
    requests: list[str] = field(default_factory=list)


# Wording an access-denied or bot-protection interstitial uses instead of content.
_CHALLENGE = re.compile(
    r"just a moment|attention required|access denied|verify you are (a )?human|"
    r"checking your browser|are you a robot|request blocked|unusual traffic|"
    r"pardon our interruption|enable javascript and cookies",
    re.IGNORECASE,
)

# Embeds that are never a roster: analytics, ads, video, maps, social widgets.
_THIRD_PARTY_FRAMES = re.compile(
    r"google|doubleclick|youtube|vimeo|facebook|twitter|linkedin|instagram|"
    r"addthis|hotjar|hubspot|zoom\.us|typeform|recaptcha|cookiebot|onetrust|trustarc",
    re.IGNORECASE,
)
MAX_FRAMES = 5
MAX_FRAME_CHARS = 400_000


def looks_like_challenge(title: str, text: str) -> str:
    """The interstitial's wording if this is a block page rather than content.

    Only a short page counts: a real page can mention "access denied" in
    passing, but a block page has nothing else on it."""
    if len((text or "").strip()) > 4000:
        return ""
    match = _CHALLENGE.search(f"{title}\n{(text or '')[:1500]}")
    return match.group(0) if match else ""


async def _frame_content(page: Page) -> tuple[str, str]:
    """HTML and text of the page's own embedded frames.

    Rosters are often an iframe pointing at a scheduling or people system, so
    the page itself is empty. The frames are read as part of the page. Frames
    from ad, analytics, video and map services never are."""
    html_parts: list[str] = []
    text_parts: list[str] = []
    for frame in page.frames[1:1 + MAX_FRAMES * 3]:
        if len(html_parts) >= MAX_FRAMES:
            break
        url = frame.url or ""
        if not url.startswith(("http://", "https://")) or _THIRD_PARTY_FRAMES.search(host_of(url)):
            continue
        try:
            body_text = await frame.evaluate("() => document.body ? document.body.innerText : ''")
            if len((body_text or "").strip()) < 200:
                continue
            content = await frame.content()
        except PlaywrightError:
            continue
        html_parts.append(f'<div data-agentscrape-frame="{url}">{content[:MAX_FRAME_CHARS]}</div>')
        text_parts.append(body_text)
    return "\n".join(html_parts), "\n".join(text_parts)


class BrowserPool:
    """One isolated context per concurrent agent, reused across sites.

    Contexts are the memory constraint on a single box, so the ceiling is
    explicit and exceeding it raises rather than letting the box thrash.
    """

    def __init__(self, size: int) -> None:
        if size > settings.max_concurrent_contexts:
            raise ValueError(
                f"Requested {size} browser contexts but MAX_CONCURRENT_CONTEXTS is "
                f"{settings.max_concurrent_contexts}."
            )
        self.size = size
        self._playwright = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",  # /dev/shm is small on cloud boxes
                "--disable-gpu",
                "--no-sandbox",
                "--disable-background-timer-throttling",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        log.info("browser launched (pool size %d)", self.size)

    async def acquire(self, agent_id: str) -> BrowserContext:
        """Get this agent's context, creating it once and reusing it thereafter."""
        if self._browser is None:
            raise RuntimeError("BrowserPool.start() was not awaited")
        async with self._lock:
            context = self._contexts.get(agent_id)
            if context is None:
                if len(self._contexts) >= self.size:
                    raise RuntimeError(
                        f"browser context pool exhausted ({self.size} in use)"
                    )
                context = await self._browser.new_context(
                    user_agent=settings.user_agent,
                    viewport={"width": 1440, "height": 1800},
                    ignore_https_errors=True,
                    java_script_enabled=True,
                )
                context.set_default_timeout(settings.page_timeout_seconds * 1000)
                self._contexts[agent_id] = context
                log.info("created browser context for agent %s", agent_id)
            return context

    async def release(self, agent_id: str) -> None:
        """Drop an agent's context, e.g. after a crash. Normal reuse keeps it."""
        async with self._lock:
            context = self._contexts.pop(agent_id, None)
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                pass

    async def stop(self) -> None:
        for agent_id in list(self._contexts):
            await self.release(agent_id)
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = self._playwright = None


async def render_page(context: BrowserContext, url: str) -> RenderResult:
    """Load and read a page in the browser."""
    await get_rate_limiter().acquire(host_of(url))
    page: Page | None = None
    try:
        page = await context.new_page()
        response = await page.goto(
            url, wait_until="domcontentloaded",
            timeout=settings.page_timeout_seconds * 1000,
        )
        # Give client-rendered rosters a chance to paint, but never block on it.
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightError:
            pass
        await _wait_for_content(page)

        status = response.status if response else None
        title = await page.title()
        html = await page.content()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")

        # A refusal is not a page. Reporting it as one let a 403 or a
        # bot-protection interstitial be read as a school with nobody listed.
        if status is not None and status >= 400:
            return RenderResult(
                url=url, final_url=page.url, title=title, html="", text="",
                ok=False, status=status, error=f"HTTP {status}",
            )
        challenge = looks_like_challenge(title, text)
        if challenge:
            return RenderResult(
                url=url, final_url=page.url, title=title, html="", text="",
                ok=False, status=status,
                error=f"blocked by an access-denied or bot-protection page ({challenge!r})",
            )

        frame_html, frame_text = await _frame_content(page)
        if frame_html:
            html = f"{html}\n{frame_html}"
            text = f"{text}\n{frame_text}"

        controls: list[dict[str, Any]] = []
        try:
            controls = await page.evaluate(_CONTROLS_JS) or []
        except PlaywrightError:
            pass

        links: list[dict[str, Any]] = []
        try:
            links = await page.evaluate(_LINKS_JS) or []
        except PlaywrightError:
            pass

        return RenderResult(
            url=url, final_url=page.url, title=title, html=html, text=text,
            controls=controls, links=links,
            ok=True, status=response.status if response else None,
        )
    except PlaywrightError as exc:
        return RenderResult(
            url=url, final_url=url, title="", html="", text="",
            ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    finally:
        if page is not None:
            try:
                await page.close()
            except PlaywrightError:
                pass


async def click_by_accessible_name(
    context: BrowserContext, url: str, role: str, name: str
) -> RenderResult:
    """Interact using role + accessible name. Never pixel coordinates.

    Reloading and re-clicking rather than holding the page open keeps each step
    independently resumable, which matters because the server can stop mid-run.
    """
    await get_rate_limiter().acquire(host_of(url))
    page: Page | None = None
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded",
                        timeout=settings.page_timeout_seconds * 1000)
        locator = page.get_by_role(role, name=name, exact=False).first  # type: ignore[arg-type]
        await locator.click(timeout=10_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightError:
            pass
        await _wait_for_content(page)
        return RenderResult(
            url=url, final_url=page.url, title=await page.title(),
            html=await page.content(),
            text=await page.evaluate("() => document.body ? document.body.innerText : ''"),
            ok=True,
        )
    except PlaywrightError as exc:
        return RenderResult(url=url, final_url=url, title="", html="", text="",
                            ok=False, error=str(exc)[:300])
    finally:
        if page is not None:
            try:
                await page.close()
            except PlaywrightError:
                pass


async def search_in_browser(
    context: BrowserContext, url: str, fields: dict[str, tuple[str, str]]
) -> RenderResult:
    """Type into a page's search inputs and submit, the way a person would.

    `fields` maps a role to (CSS selector, text). Every GET request the page
    makes afterwards is recorded, so a JavaScript directory's own search API
    can be found and then called directly without the browser.
    """
    await get_rate_limiter().acquire(host_of(url))
    page: Page | None = None
    requests: list[str] = []
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded",
                        timeout=settings.page_timeout_seconds * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightError:
            pass
        page.on(
            "request",
            lambda request: requests.append(request.url) if request.method == "GET" else None,
        )
        last = None
        for selector, text in fields.values():
            last = page.locator(selector).first
            await last.fill(text, timeout=10_000)
        if last is not None:
            await last.press("Enter")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightError:
            pass
        await _wait_for_content(page)
        return RenderResult(
            url=url, final_url=page.url, title=await page.title(),
            html=await page.content(),
            text=await page.evaluate("() => document.body ? document.body.innerText : ''"),
            ok=True, requests=list(dict.fromkeys(requests)),
        )
    except PlaywrightError as exc:
        return RenderResult(url=url, final_url=url, title="", html="", text="",
                            ok=False, error=str(exc)[:300])
    finally:
        if page is not None:
            try:
                await page.close()
            except PlaywrightError:
                pass
