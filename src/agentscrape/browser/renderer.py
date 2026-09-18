"""Playwright rendering — the expensive path, used only on escalation.

Two deliberate reliability decisions live here:

  * Interaction targets come from the **accessibility tree** (role + name), never
    from pixel coordinates predicted off a screenshot. Vision reads and
    disambiguates; it does not produce click targets.
  * Field locations are measured from the real DOM with getBoundingClientRect,
    not guessed by the model, so the frontend's highlight boxes are exact.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from ..config import settings
from ..urls import host_of
from .ratelimit import get_rate_limiter

log = logging.getLogger("agentscrape.render")

# Locates the on-screen rectangle of each needle string. Runs in the page.
_LOCATE_JS = """
(needles) => {
  const out = {};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  const rectOf = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    return r && r.width > 0 && r.height > 0
      ? {x: Math.round(r.x + window.scrollX), y: Math.round(r.y + window.scrollY),
         width: Math.round(r.width), height: Math.round(r.height)}
      : null;
  };
  for (const needle of needles) {
    if (!needle) continue;
    const lower = needle.toLowerCase();
    for (const node of nodes) {
      const text = (node.textContent || '').toLowerCase();
      if (text.includes(lower)) { const r = rectOf(node); if (r) { out[needle] = r; break; } }
    }
    if (!out[needle]) {
      const link = document.querySelector(`a[href*="${CSS.escape(needle)}"]`);
      if (link) {
        const r = link.getBoundingClientRect();
        if (r && r.width > 0) out[needle] = {x: Math.round(r.x + window.scrollX),
          y: Math.round(r.y + window.scrollY), width: Math.round(r.width), height: Math.round(r.height)};
      }
    }
  }
  return out;
}
"""

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
    screenshot: bytes | None = None
    field_locations: dict[str, dict[str, int]] = field(default_factory=dict)
    controls: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    status: int | None = None


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


async def render_page(
    context: BrowserContext,
    url: str,
    *,
    capture_screenshot: bool = True,
    locate: list[str] | None = None,
    full_page: bool = True,
) -> RenderResult:
    """Load a page, optionally screenshot it, and measure where fields sit."""
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

        title = await page.title()
        html = await page.content()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")

        locations: dict[str, dict[str, int]] = {}
        if locate:
            try:
                locations = await page.evaluate(_LOCATE_JS, locate[:200]) or {}
            except PlaywrightError as exc:
                log.debug("field location lookup failed on %s: %s", url, exc)

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

        shot: bytes | None = None
        if capture_screenshot:
            try:
                shot = await page.screenshot(full_page=full_page, type="png")
            except PlaywrightError as exc:
                # Very tall pages can exceed the capture limit; fall back to viewport.
                log.debug("full-page screenshot failed on %s (%s); viewport only", url, exc)
                try:
                    shot = await page.screenshot(full_page=False, type="png")
                except PlaywrightError:
                    shot = None

        return RenderResult(
            url=url, final_url=page.url, title=title, html=html, text=text,
            screenshot=shot, field_locations=locations, controls=controls, links=links,
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
