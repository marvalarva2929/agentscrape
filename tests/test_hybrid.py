"""Hybrid strategy: the HTML pass maps the site so the model reads less."""

from __future__ import annotations

from sqlalchemy import select

from agentscrape.db.models import Record, Site
from agentscrape.llm import provider as provider_module
from agentscrape.llm.prompts import READER_SYSTEM, TRIAGE_SYSTEM
from agentscrape.pipeline.nodes.html_map import people_signal

from .fixture_server import ALUMNI, DEFAULT_RESIDENTS, FELLOWS, NEWS, residents_page, serve
from .test_end_to_end import _run_once, fast_and_offline  # noqa: F401  (autouse fixture)


class CountingOfflineProvider:
    """Counts what the pipeline asks the model for; answers nothing, so every
    page falls back to the HTML extractor and both strategies see the same
    people."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.triage_calls = 0

    async def complete(self, **kwargs):
        if kwargs.get("system") is READER_SYSTEM:
            self.reads.append(kwargs["user"])
        elif kwargs.get("system") is TRIAGE_SYSTEM:
            self.triage_calls += 1
        raise ConnectionError("offline")


async def _crawl(session, monkeypatch, strategy: str) -> tuple[CountingOfflineProvider, set[str]]:
    counting = CountingOfflineProvider()
    monkeypatch.setattr(provider_module, "_provider", counting)
    # A host per strategy: sites are keyed by hostname, and a second crawl of
    # the same one would find the first strategy's people already stored.
    with serve(hostname=f"{strategy}.localhost") as site:
        await _run_once([site.base], session=session, crawl_strategy=strategy)
    site_row = await session.scalar(select(Site).where(Site.root_domain == f"{strategy}.localhost"))
    names = {
        r.full_name
        for r in (await session.execute(select(Record).where(Record.site_id == site_row.id))).scalars()
    }
    return counting, names


async def test_hybrid_finds_the_same_people_with_fewer_model_reads(session, monkeypatch):
    agent, agent_names = await _crawl(session, monkeypatch, "agent")
    hybrid, hybrid_names = await _crawl(session, monkeypatch, "hybrid")

    trainees = {"Ann Riley", "Ben Cole", "Cara Diaz", "Dana Fields"}
    assert trainees <= agent_names and trainees <= hybrid_names
    # A page with one name and one address is a contact block, not a roster;
    # the hybrid gate leaves it unread (here, the one-person alumni page).
    assert agent_names - hybrid_names == {"Gone Person"}
    # The news page shows nobody in its HTML, so the model never reads it.
    assert any("/news" in prompt for prompt in agent.reads)
    assert not any("/news" in prompt for prompt in hybrid.reads)
    assert not any("/alumni" in prompt for prompt in hybrid.reads)
    assert len(hybrid.reads) < len(agent.reads)
    # Sitemap URLs are ranked by the heuristic, not sent to the model.
    assert hybrid.triage_calls < agent.triage_calls


def test_the_people_signal_is_generous_but_not_indiscriminate():
    assert people_signal(residents_page(DEFAULT_RESIDENTS), "https://x.edu/residents", "Residents").keep
    assert people_signal(FELLOWS, "https://x.edu/fellows", "Cardiology Fellows").keep
    assert not people_signal(NEWS, "https://x.edu/news", "News").keep
    assert not people_signal(ALUMNI, "https://x.edu/alumni", "Alumni").keep
