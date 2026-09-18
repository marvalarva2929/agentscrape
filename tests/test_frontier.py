"""The work list grows as pages are read, ordered by the model's priority.

Discovery runs once, before anything is fetched, so it cannot see a page that is
only reachable by following a link. Links off every page are triaged by the
model and folded into the unvisited tail without disturbing the cursor. Nothing
is dropped for lacking a keyword: BCM's CA-3 anesthesia roster sat behind a
seven-token slug that the old score floor threw away.
"""

from __future__ import annotations

import json

import pytest

from agentscrape.llm.provider import ModelResponse, VisionProvider
from agentscrape.llm.triage import heuristic_priority, triage_links
from agentscrape.llm.usage import Usage
from agentscrape.pipeline.nodes.extract import merge_frontier

CA3 = (
    "https://www.bcm.edu/departments/anesthesiology/education/"
    "anesthesiology-residency/residents/clinical-anesthesia-year-3-pgy-4-1"
)


def candidate(url: str, priority: float, score: float = 0.0) -> dict:
    return {"url": url, "score": score, "priority": priority, "is_known_path": False}


class TriageProvider(VisionProvider):
    def __init__(self, verdicts: dict[int, object]) -> None:
        self.verdicts = verdicts
        self.prompts: list[str] = []

    async def complete(self, **kwargs) -> ModelResponse:
        self.prompts.append(kwargs["user"])
        body = {"links": [{"i": i, "p": p} for i, p in self.verdicts.items()]}
        return ModelResponse(text=json.dumps(body), usage=Usage(), model="fake")


def test_higher_priority_additions_go_ahead_of_the_queue() -> None:
    queued = [candidate("https://x.edu/faculty", 40), candidate("https://x.edu/team", 35)]
    merged = merge_frontier(queued, 0, [candidate(CA3, 98)])
    assert merged[0]["url"] == CA3


def test_visited_candidates_keep_their_positions() -> None:
    """The caller's cursor indexes this list, so the head must not move."""
    visited = [candidate(f"https://x.edu/seen/{i}", 10) for i in range(3)]
    merged = merge_frontier(
        [*visited, candidate("https://x.edu/queued", 20)], 3, [candidate(CA3, 98)]
    )
    assert [c["url"] for c in merged[:3]] == [c["url"] for c in visited]
    assert merged[3]["url"] == CA3


def test_case_variants_of_a_queued_or_visited_page_are_not_added() -> None:
    visited = [candidate("https://x.edu/GME/Residents", 90)]
    merged = merge_frontier(visited, 1, [candidate("https://x.edu/gme/residents", 95)])
    assert len(merged) == 1


def test_a_rediscovered_link_keeps_its_best_priority_and_program() -> None:
    queued = [{**candidate(CA3, 60), "program": "Anesthesiology Residency"}]
    merged = merge_frontier(queued, 0, [{**candidate(CA3, 97), "program": None}])
    assert merged[0]["priority"] == 97
    assert merged[0]["program"] == "Anesthesiology Residency"


@pytest.mark.asyncio
async def test_model_keeps_a_roster_the_keyword_score_rejected() -> None:
    provider = TriageProvider({0: 98, 1: "skip"})
    links = [
        {"url": CA3, "text": "Clinical Anesthesia Year 3 (PGY-4)", "in_nav": True},
        {"url": "https://www.bcm.edu/giving", "text": "Give now"},
    ]
    decisions = await triage_links(links, source="test", provider=provider)
    assert decisions[0].priority == 98 and decisions[0].by_model
    assert decisions[0].heuristic < 6.0  # the old frontier floor
    assert decisions[1].skipped
    # The anchor text is what the model judges by.
    assert "Clinical Anesthesia Year 3 (PGY-4)" in provider.prompts[0]


@pytest.mark.asyncio
async def test_links_the_model_does_not_mention_are_kept() -> None:
    decisions = await triage_links(
        [{"url": CA3}, {"url": "https://www.bcm.edu/about"}],
        source="test", provider=TriageProvider({0: 90}),
    )
    assert not decisions[1].skipped
    assert not decisions[1].by_model


@pytest.mark.asyncio
async def test_a_failed_triage_call_keeps_everything_by_heuristic() -> None:
    """The conftest provider is offline: nothing may be lost to that."""
    decisions = await triage_links([{"url": CA3}, {"url": "https://x.edu/a"}], source="test")
    assert all(not d.skipped and not d.by_model for d in decisions)
    assert decisions[0].priority == heuristic_priority(decisions[0].heuristic)
