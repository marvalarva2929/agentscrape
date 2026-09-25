"""The role verifier accepts one grounded category per person."""

from __future__ import annotations

import json

import pytest

from agentscrape.llm.provider import ModelResponse, VisionProvider
from agentscrape.llm.usage import Usage
from agentscrape.llm.verify import RoleCheckInput, verify_page_roles
from agentscrape.verification.service import _deterministic_current_role, _verification_quality


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
    assert len(provider.calls) == 1
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


@pytest.mark.asyncio
async def test_research_fellow_evidence_cannot_verify_a_gme_fellow() -> None:
    provider = RoleProvider({"people": [{
        "name": "Mina Shah", "role": "fellow", "evidence": "Research Fellow",
    }]})
    result = await verify_page_roles(
        url="https://med.example.edu/people", title="People",
        text="## Research Fellows\nMina Shah, Research Fellow",
        people=[RoleCheckInput("1", "Mina Shah", "fellow")], provider=provider,
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


class _RaisingProvider(VisionProvider):
    """A hard provider/model failure - never a usable response at all."""

    async def complete(self, **kwargs):
        raise RuntimeError("model not supported by any provider you have enabled")


@pytest.mark.asyncio
async def test_hard_provider_failure_returns_none_not_empty_dict() -> None:
    """A call that never produced a response must be distinguishable from one
    that answered but grounded nobody: the caller maps `None` to
    VERIFICATION_ERROR and `{}` to INSUFFICIENT_EVIDENCE - conflating them was
    exactly what hid the Qwen model_not_supported failures as ordinary
    "no source-backed role decision" results."""
    result = await verify_page_roles(
        url="https://med.example.edu/people", title="People", text="Mina Shah",
        people=[RoleCheckInput("1", "Mina Shah", "resident")],
        provider=_RaisingProvider(),
    )
    assert result is None


def test_duplicate_name_cannot_auto_confirm_until_disambiguated() -> None:
    score, risk, reason = _verification_quality(
        role="resident", evidence="Current Residents", position=None,
        url="https://med.example.edu/residents", title="Current Residents", duplicate_name=True,
    )
    assert (score, risk) == (0.15, "high")
    assert "Multiple records" in reason


@pytest.mark.parametrize(
    ("category", "text", "expected"),
    [
        ("resident", "## Current Residents\nMina Shah — Internal Medicine Resident", "resident"),
        ("resident", "## PGY-2\nMina Shah", "resident"),
        ("fellow", "## Current Cardiology Fellows\nMina Shah", "fellow"),
    ],
)
def test_matching_current_roster_evidence_is_deterministic(category, text, expected) -> None:
    decision = _deterministic_current_role(RoleCheckInput("1", "Mina Shah", category), text)
    assert decision is not None
    assert decision.role == expected


@pytest.mark.parametrize(
    "text",
    [
        "Mina Shah was nominated for Resident of the Year.",
        "## Alumni\nMina Shah, former resident",
        "## Residents 2021\nMina Shah completed residency here in 2022",
    ],
)
def test_articles_alumni_and_historical_pages_never_auto_confirm(text) -> None:
    assert _deterministic_current_role(RoleCheckInput("1", "Mina Shah", "resident"), text) is None


def test_committee_heading_never_auto_confirms_a_resident() -> None:
    decision = _deterministic_current_role(
        RoleCheckInput("1", "Mina Shah", "resident"),
        "## Resident Advisory Committee\nMina Shah",
    )
    assert decision is not None
    assert decision.role == "unknown"


@pytest.mark.parametrize(
    "heading",
    [
        "Resident Advisory Council",
        "GME Quality Improvement Task Force",
        "Fellowship Steering Committee",
        "Resident Representative Working Group",
        "Research Fellows",
    ],
)
def test_non_roster_context_corrects_a_legacy_trainee_label_to_unknown(heading) -> None:
    decision = _deterministic_current_role(
        RoleCheckInput("1", "Mina Shah", "resident"), f"## {heading}\nMina Shah",
    )
    assert decision is not None
    assert decision.role == "unknown"


def test_governance_context_preserves_explicit_current_gme_evidence() -> None:
    assert _deterministic_current_role(
        RoleCheckInput("1", "Mina Shah", "resident"),
        "## Resident Advisory Council\nMina Shah, PGY-3 Resident Physician",
    ) is None
