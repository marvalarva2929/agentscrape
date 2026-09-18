"""The model reads pages; deterministic guardrails keep what it says honest."""

from __future__ import annotations

import json

import pytest

from agentscrape.config import settings
from agentscrape.db.enums import PersonCategory
from agentscrape.extraction.person import ExtractedPerson
from agentscrape.extraction.text import chunk_text, html_to_model_text
from agentscrape.llm.planner import FOUND, PENDING, match_program
from agentscrape.llm.provider import ModelResponse, VisionProvider
from agentscrape.llm.reader import combine_with_regex, fold, read_page
from agentscrape.llm.usage import LLMUnavailable, Usage, UsageMeter
from agentscrape.pipeline.nodes.extract import PageOutcome, _update_programs


class ReaderProvider(VisionProvider):
    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    async def complete(self, **kwargs) -> ModelResponse:
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls), len(self.payloads)) - 1]
        return ModelResponse(text=json.dumps(payload), usage=Usage(10, 10), model="fake")


ROSTER_TEXT = """## PGY-2
[image: Lorenzo Canseco, MD]
## Lorenzo Canseco, MD
University of Texas Medical School at San Antonio <lcanseco@bswhealth.org>
## JANE ROE, DO
"""


@pytest.mark.asyncio
async def test_invented_people_and_addresses_are_dropped() -> None:
    provider = ReaderProvider({
        "page_type": "roster", "is_current_trainee_roster": True,
        "expected_people_count": 2,
        "people": [
            {"full_name": "Lorenzo Canseco, MD", "email": "lcanseco@bswhealth.org",
             "category": "resident", "pgy": "2"},
            {"full_name": "JANE ROE", "email": "jane.roe@bswhealth.org", "category": "resident"},
            {"full_name": "Invented Person", "category": "resident"},
        ],
    })
    reading = await read_page(url="https://x.org/r", title="Residents", text=ROSTER_TEXT, provider=provider)
    assert reading.ok and reading.is_current_trainee_roster
    names = {p.full_name: p for p in reading.people}
    assert set(names) == {"Lorenzo Canseco", "Jane Roe"}
    assert names["Lorenzo Canseco"].pgy == 2
    assert names["Lorenzo Canseco"].category == PersonCategory.RESIDENT
    # Not on the page, so not kept - the person is, the address is not.
    assert names["Jane Roe"].email is None


@pytest.mark.asyncio
async def test_long_pages_are_read_in_chunks_and_merged(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_page_chunk_chars", 200)
    letters = "abcdefghijklmnopqrstuvwxyzabcdefghijklmn"
    text = "\n".join(f"## Person{c} Surname{c}{i % 3 * 'x'}" for i, c in enumerate(letters))
    provider = ReaderProvider(
        {"people": [{"full_name": "Persona Surnamea", "category": "resident"}],
         "expected_people_count": 1},
    )
    reading = await read_page(url="https://x.org/r", title="t", text=text, provider=provider)
    assert len(provider.calls) == len(chunk_text(text, 200)) > 1
    assert "part 2 of" in provider.calls[1]["user"]
    # The same person reported by every chunk is one person.
    assert len(reading.people) == 1


@pytest.mark.asyncio
async def test_a_failed_read_reports_not_ok_so_the_caller_falls_back() -> None:
    reading = await read_page(url="https://x.org/r", title="t", text=ROSTER_TEXT)
    assert not reading.ok


@pytest.mark.asyncio
async def test_a_dead_endpoint_aborts_instead_of_degrading(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_max_consecutive_failures", 3)
    meter = UsageMeter()
    with pytest.raises(LLMUnavailable):
        for _ in range(3):
            await read_page(url="https://x.org/r", title="t", text=ROSTER_TEXT, meter=meter)
    assert meter.failures == 3


def test_regex_people_survive_only_as_the_address_that_anchors_them() -> None:
    model = [ExtractedPerson(full_name="Lorenzo Canseco", category=PersonCategory.RESIDENT)]
    regex = [
        ExtractedPerson(full_name="Lorenzo Canseco", email="lcanseco@bswhealth.org"),
        ExtractedPerson(full_name="Residency Navigation"),
        ExtractedPerson(full_name="Pat Doe", email="pat.doe@bswhealth.org"),
    ]
    text = fold(ROSTER_TEXT + " pat.doe@bswhealth.org")
    kept = combine_with_regex(model, regex, text)
    assert [p.email for p in kept] == ["lcanseco@bswhealth.org", "pat.doe@bswhealth.org"]
    assert kept[0].full_name == "Lorenzo Canseco"
    assert kept[0].category == PersonCategory.RESIDENT
    # The address is on the page; the name and role are regex guesses.
    assert kept[1].full_name is None
    assert kept[1].category == PersonCategory.UNKNOWN


def test_page_text_keeps_hidden_tabs_mailto_alt_text_and_embedded_people() -> None:
    html = """<html><body>
      <nav><a href="/x">Menu</a></nav>
      <div class="tab" style="display:none"><h3>PGY-3</h3>
        <div><img alt="Headshot of Ann Lee"><a href="mailto:ann.lee@x.edu">Email</a></div>
      </div>
      <script>window.data = [{"displayFirstName":"Bo","displayLastName":"Chen","title":"PGY-1"},
        {"displayFirstName":"Cy","displayLastName":"Diaz","title":"PGY-1"},
        {"displayFirstName":"Di","displayLastName":"Eng","title":"PGY-2"}]</script>
    </body></html>"""
    text = html_to_model_text(html)
    assert "## PGY-3" in text
    assert "[image: Headshot of Ann Lee]" in text
    assert "<ann.lee@x.edu>" in text
    assert "displayLastName=Chen" in text
    assert "Menu" not in text


def test_a_trainee_roster_marks_its_program_covered() -> None:
    programs = [
        {"name": "Anesthesiology Residency", "kind": "residency",
         "landing_url": "https://x.edu/anesthesia/residency", "status": PENDING},
        {"name": "Pediatric Anesthesiology Fellowship", "kind": "fellowship",
         "landing_url": None, "status": PENDING},
    ]
    assert match_program(programs, "Anesthesiology Fellowship - Pediatric") is programs[1]
    outcome = PageOutcome(url="https://x.edu/anesthesia/residency/residents/ca-3")
    outcome.people = [ExtractedPerson(full_name=f"A B{i}", category=PersonCategory.RESIDENT) for i in range(4)]
    _update_programs(programs, {"url": outcome.url}, outcome, trainees=4)
    assert programs[0]["status"] == FOUND
    assert programs[1]["status"] == PENDING
