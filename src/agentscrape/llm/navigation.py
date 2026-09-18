"""Multimodal judgment for the next action on a rendered page."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..urls import canonicalize
from .prompts import NAVIGATION_SYSTEM, navigation_user_prompt
from .provider import VisionProvider, get_provider
from .usage import UsageMeter

log = logging.getLogger("agentscrape.llm.navigation")


@dataclass(frozen=True)
class NavigationDecision:
    page_type: str = "unknown"
    control: dict[str, str] | None = None
    visit_urls: tuple[str, ...] = ()
    reason: str = ""


async def decide_navigation(
    *,
    url: str,
    title: str,
    text: str,
    links: list[dict],
    controls: list[dict],
    screenshot: bytes | None,
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
) -> NavigationDecision:
    """Ask the model how to continue, accepting only supplied actions and URLs."""
    provider = provider or get_provider()
    prompt = navigation_user_prompt(
        url=url, title=title, text=text, controls=controls, links=links
    )
    try:
        response = await provider.complete(
            system=NAVIGATION_SYSTEM,
            user=prompt,
            image_bytes=screenshot,
            meter=meter,
            max_tokens=1_000,
        )
        payload = response.json()
    except Exception as exc:
        log.warning("navigation decision failed for %s: %s", url, exc)
        return NavigationDecision()
    if not isinstance(payload, dict):
        return NavigationDecision()

    available_controls = {
        (str(c.get("role", "")), str(c.get("name", ""))): c
        for c in controls
        if not c.get("disabled")
    }
    chosen_control = None
    raw_control = payload.get("control")
    if isinstance(raw_control, dict):
        key = (str(raw_control.get("role", "")), str(raw_control.get("name", "")))
        supplied = available_controls.get(key)
        if supplied is not None:
            chosen_control = {"role": key[0], "name": key[1]}

    available_urls: dict[str, str] = {}
    for link in links:
        raw_url = link.get("url")
        if not isinstance(raw_url, str):
            continue
        canonical = canonicalize(raw_url)
        if canonical:
            available_urls[canonical] = canonical

    selected: list[str] = []
    raw_urls = payload.get("visit_urls")
    if isinstance(raw_urls, list):
        for raw_url in raw_urls:
            canonical = canonicalize(raw_url) if isinstance(raw_url, str) else None
            if canonical and canonical in available_urls and canonical not in selected:
                selected.append(canonical)

    return NavigationDecision(
        page_type=str(payload.get("page_type", "unknown"))[:40],
        control=chosen_control,
        visit_urls=tuple(selected),
        reason=str(payload.get("reason", ""))[:500],
    )
