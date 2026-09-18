"""Which of an institution's hosts to explore is the model's decision.

A hostname deny-list ruled out `emergency.<univ>.edu` because "emergency" was
listed as campus safety. The model sees each host with sample paths instead, and
only a host it explicitly rejects is left unwalked.
"""

from __future__ import annotations

import json

import pytest

from agentscrape.llm.provider import ModelResponse, VisionProvider
from agentscrape.llm.triage import triage_hosts
from agentscrape.llm.usage import Usage
from agentscrape.pipeline.nodes.discover import _observed_host_counts

ROOT = "medicine.arizona.edu"


class HostProvider(VisionProvider):
    def __init__(self, keep: dict[str, bool]) -> None:
        self.keep = keep
        self.prompt = ""

    async def complete(self, **kwargs) -> ModelResponse:
        self.prompt = kwargs["user"]
        hosts = [line.split(". ", 1)[1].split()[0] for line in self.prompt.splitlines() if ". " in line and line[0].isdigit()]
        body = {"hosts": [{"i": i, "keep": self.keep.get(h, True)} for i, h in enumerate(hosts)]}
        return ModelResponse(text=json.dumps(body), usage=Usage(), model="fake")


@pytest.mark.asyncio
async def test_only_hosts_the_model_rejects_are_skipped() -> None:
    samples = {
        "emergency.arizona.edu": ["/residency/current-residents"],
        "bookstore.arizona.edu": ["/textbooks"],
        "imr.bsd.arizona.edu": [],
    }
    provider = HostProvider({"bookstore.arizona.edu": False})
    skipped = await triage_hosts(samples, root_domain=ROOT, provider=provider)
    assert skipped == {"bookstore.arizona.edu"}
    assert "/residency/current-residents" in provider.prompt


@pytest.mark.asyncio
async def test_a_failed_host_triage_keeps_every_host() -> None:
    skipped = await triage_hosts({"surgery.arizona.edu": ["/"]}, root_domain=ROOT)
    assert skipped == set()


def test_observed_hosts_are_in_scope_siblings_only() -> None:
    urls = dict.fromkeys([
        f"https://{ROOT}/a",
        "https://obgyn.arizona.edu/residents",
        "https://obgyn.arizona.edu/fellows",
        "https://residents.acgme.org/x",
    ])
    assert _observed_host_counts(urls, ROOT, {"arizona.edu"}) == {"obgyn.arizona.edu": 2}
