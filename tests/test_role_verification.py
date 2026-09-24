"""The role verifier accepts one grounded category per person."""

from __future__ import annotations

import json

import pytest

from agentscrape.llm.provider import ModelResponse, VisionProvider
from agentscrape.llm.usage import Usage
from agentscrape.llm.verify import RoleCheckInput, verify_page_roles


class RoleProvider(VisionProvider):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def complete(self, **kwargs) -> ModelResponse:
        self.calls.append(kwargs)
        return ModelResponse(text=json.dumps(self.payload), usage=Usage(10, 10), model="fake")


@pytest.mark.asyncio
async def test_verification_keeps_one_grounded_role_and_allows_alumni() -> None:
    provider = RoleProvider({"people": [
        {"name": "Mina Shah", "role": "resident"},
        {"name": "Alex Chen", "role": "alumni"},
    ]})
    people = [
        RoleCheckInput("resident", "Mina Shah", "fellow"),
        RoleCheckInput("alumnus", "Alex Chen", "resident"),
    ]

    result = await verify_page_roles(
        url="https://med.example.edu/people", title="People",
        text="## Current Residents\nMina Shah\n## Alumni\nAlex Chen", people=people,
        provider=provider,
    )

    assert result == {"resident": "resident", "alumnus": "alumni"}
    assert '"role"' in provider.calls[0]["system"]
    assert "fellowship roster makes the person a fellow" in provider.calls[0]["system"]


@pytest.mark.asyncio
async def test_verification_rejects_multi_role_or_unknown_names() -> None:
    provider = RoleProvider({"people": [
        {"name": "Mina Shah", "role": ["resident", "faculty"]},
        {"name": "Not On Page", "role": "resident"},
    ]})

    result = await verify_page_roles(
        url="https://med.example.edu/people", title="Residents", text="Mina Shah",
        people=[RoleCheckInput("resident", "Mina Shah", "resident")], provider=provider,
    )

    assert result == {}
