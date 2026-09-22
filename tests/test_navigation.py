"""The navigation model sees the rendered page and can only choose real actions."""

from __future__ import annotations

import json

import pytest

from agentscrape.llm.navigation import decide_navigation
from agentscrape.llm.provider import ModelResponse, VisionProvider
from agentscrape.llm.usage import Usage


class FakeProvider(VisionProvider):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.request: dict | None = None

    async def complete(self, **kwargs) -> ModelResponse:
        self.request = kwargs
        return ModelResponse(text=json.dumps(self.payload), usage=Usage(), model="fake")


@pytest.mark.asyncio
async def test_navigation_decision_includes_page_context() -> None:
    provider = FakeProvider(
        {
            "page_type": "program",
            "control": {"role": "button", "name": "Load more residents"},
            "visit_urls": [
                "https://medicine.example.edu/current-residents",
                "https://hallucinated.example.edu/roster",
            ],
            "reason": "The roster button and current-residents link are promising.",
        }
    )
    decision = await decide_navigation(
        url="https://medicine.example.edu/program",
        title="Internal Medicine Residency",
        text="Welcome to the residency program.",
        links=[
            {
                "url": "https://medicine.example.edu/current-residents",
                "text": "Current Residents",
                "context": "Meet our current residents",
            }
        ],
        controls=[
            {"role": "button", "name": "Load more residents", "disabled": False}
        ],
        provider=provider,
    )

    assert provider.request is not None
    assert "Internal Medicine Residency" in provider.request["user"]
    assert "Current Residents" in provider.request["user"]
    assert decision.control == {"role": "button", "name": "Load more residents"}
    assert decision.visit_urls == ("https://medicine.example.edu/current-residents",)


@pytest.mark.asyncio
async def test_navigation_rejects_controls_not_supplied_by_browser() -> None:
    provider = FakeProvider(
        {
            "page_type": "program",
            "control": {"role": "button", "name": "Invented action"},
            "visit_urls": [],
            "reason": "",
        }
    )
    decision = await decide_navigation(
        url="https://example.edu/",
        title="Program",
        text="Program page",
        links=[],
        controls=[{"role": "button", "name": "Real action", "disabled": False}],
        provider=provider,
    )
    assert decision.control is None
