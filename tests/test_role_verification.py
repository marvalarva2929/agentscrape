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


@pytest.mark.asyncio
async def test_verification_keeps_only_a_source_quoted_sane_position() -> None:
    provider = RoleProvider({"people": [{
        "name": "Mina Shah", "role": "resident", "evidence": "PGY-2 Resident",
        "position": "PGY-2 Resident", "position_evidence": "Mina Shah, PGY-2 Resident",
    }]})
    result = await verify_page_roles(
        url="https://med.example.edu/people", title="Residents",
        text="## Current Residents\nMina Shah, PGY-2 Resident",
        people=[RoleCheckInput("1", "Mina Shah", "resident")], provider=provider,
    )
    assert result["1"].position == "PGY-2 Resident"


@pytest.mark.asyncio
async def test_verification_rejects_a_paper_title_as_position() -> None:
    paper = "A consequentialist ethical analysis of federal funding of elective abortions"
    provider = RoleProvider({"people": [{
        "name": "Emile I Gleeson", "role": "student", "evidence": "Student",
        "position": paper, "position_evidence": paper,
    }]})
    result = await verify_page_roles(
        url="https://med.example.edu/profile", title="Profile",
        text=f"Emile I Gleeson, Student\n{paper}",
        people=[RoleCheckInput("1", "Emile I Gleeson", "student")], provider=provider,
    )
    assert result["1"].position is None


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


def _check(text: str, category: str = "resident", **kwargs):
    from agentscrape.verification.evidence import check_against_crawl

    return check_against_crawl(
        text=text, url=kwargs.pop("url", "https://med.example.edu/people"),
        title=kwargs.pop("title", ""), full_name="Mina Shah", category=category, **kwargs,
    )


@pytest.mark.parametrize(
    ("text", "category", "title"),
    [
        ("## Current Residents\nMina Shah\nMD, Emory", "resident", ""),
        ("Mina Shah, PGY-3", "resident", ""),
        ("## Cardiology Fellows\nMina Shah, PGY-5", "fellow", ""),
        ("Mina Shah\nMedical School: Emory", "resident", "Internal Medicine Residents"),
        ("## Alumni\nMina Shah, Class of 2019", "alumni", ""),
    ],
)
def test_a_page_that_plainly_supports_the_crawl_label_agrees(text, category, title) -> None:
    check = _check(text, category, title=title)
    assert check.verdict == "agree", check.reason
    assert check.evidence


@pytest.mark.parametrize(
    "text",
    [
        # The next person's PGY year is not this person's evidence.
        "## Our People\nMina Shah\nOmar Diaz, PGY-2",
        "## Residents and Faculty\nMina Shah",
        "## Current Residents\nMina Shah, Chief Resident\n## Former Residents\nMina Shah, 2018",
        "## Graduating Residents\nMina Shah",
        "## Resident Award Nominees\nMina Shah, PGY-2",
        "## Incoming Interns\nMina Shah",
    ],
)
def test_conflicting_or_questionable_context_goes_to_the_model(text) -> None:
    assert _check(text).verdict == "ambiguous"


def test_a_label_with_nothing_to_check_goes_to_the_model() -> None:
    assert _check("## Current Residents\nMina Shah", "unknown").verdict == "ambiguous"


def test_a_conflicting_printed_position_goes_to_the_model() -> None:
    check = _check("## Current Residents\nMina Shah", position="Assistant Professor")
    assert check.verdict == "ambiguous"


def test_a_name_not_on_the_page_is_absent() -> None:
    assert _check("## Current Residents\nOmar Diaz").verdict == "absent"
