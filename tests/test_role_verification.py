"""The role verifier accepts one grounded category per person."""

from __future__ import annotations

import json

import pytest

from agentscrape.llm.provider import ModelResponse, VisionProvider
from agentscrape.llm.usage import Usage
from agentscrape.llm.verify import RoleCheckInput, verify_page_roles
from agentscrape.verification.service import _verification_quality


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
        {"name": "Mina Shah", "role": "resident", "evidence": "Current Residents"},
        {"name": "Alex Chen", "role": "alumni", "evidence": "Alumni"},
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

    assert {record_id: decision.role for record_id, decision in result.items()} == {
        "resident": "resident", "alumnus": "alumni",
    }
    assert '"role"' in provider.calls[0]["system"]
    assert "fellowship roster makes the person a fellow" in provider.calls[0]["system"]


@pytest.mark.asyncio
async def test_verification_rejects_multi_role_or_unknown_names() -> None:
    provider = RoleProvider({"people": [
        {"name": "Mina Shah", "role": ["resident", "faculty"], "evidence": "Residents"},
        {"name": "Not On Page", "role": "resident", "evidence": "Residents"},
    ]})

    result = await verify_page_roles(
        url="https://med.example.edu/people", title="Residents", text="Mina Shah",
        people=[RoleCheckInput("resident", "Mina Shah", "resident")], provider=provider,
    )

    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "role", "evidence"),
    [
        ("## Current Residents\nMina Shah, PGY-2", "resident", "PGY-2"),
        ("## Cardiology Fellows\nMina Shah, PGY-5", "fellow", "Cardiology Fellows"),
        ("## Alumni\nMina Shah, former resident", "alumni", "former resident"),
    ],
)
async def test_resident_fellow_and_alumni_decisions_require_local_evidence(text, role, evidence) -> None:
    provider = RoleProvider({"people": [{"name": "Mina Shah", "role": role, "evidence": evidence}]})
    result = await verify_page_roles(
        url="https://med.example.edu/people", title="People", text=text,
        people=[RoleCheckInput("1", "Mina Shah", "unknown")], provider=provider,
    )
    assert result["1"].role == role


@pytest.mark.asyncio
async def test_resident_cannot_be_supported_by_former_resident_evidence() -> None:
    provider = RoleProvider({"people": [{
        "name": "Mina Shah", "role": "resident", "evidence": "former resident",
    }]})
    result = await verify_page_roles(
        url="https://med.example.edu/people", title="Alumni",
        text="## Alumni\nMina Shah, former resident",
        people=[RoleCheckInput("1", "Mina Shah", "resident")], provider=provider,
    )
    assert result == {}


def test_article_and_conflicting_positions_cannot_auto_confirm_trainees() -> None:
    score, risk, reason = _verification_quality(
        role="resident", evidence="Current Residents", position=None,
        url="https://med.example.edu/news/resident-award", title="Resident award",
    )
    assert (score, risk) == (0.35, "high")
    assert "article" in reason

    score, risk, reason = _verification_quality(
        role="fellow", evidence="Fellows", position="Associate Professor",
        url="https://med.example.edu/fellows", title="Current fellows",
    )
    assert (score, risk) == (0.20, "high")
    assert "conflicts" in reason


def test_duplicate_name_cannot_auto_confirm_until_disambiguated() -> None:
    score, risk, reason = _verification_quality(
        role="resident", evidence="Current Residents", position=None,
        url="https://med.example.edu/residents", title="Current Residents", duplicate_name=True,
    )
    assert (score, risk) == (0.15, "high")
    assert "Multiple records" in reason
