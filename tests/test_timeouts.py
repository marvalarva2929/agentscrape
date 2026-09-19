"""Gateway timeouts: retried once, then the request is made smaller, not resent."""

from __future__ import annotations

import json

import httpx
import pytest
from openai import APIStatusError

from agentscrape.config import settings
from agentscrape.llm.provider import (
    ModelResponse,
    ModelTimeout,
    OpenAICompatibleProvider,
    VisionProvider,
)
from agentscrape.llm.reader import read_page
from agentscrape.llm.triage import triage_links
from agentscrape.llm.usage import Usage, UsageMeter


def _status_error(code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://router.example/v1/chat/completions")
    response = httpx.Response(code, request=request)
    return APIStatusError(f"HTTP {code}", response=response, body=None)


class _FailingCompletions:
    def __init__(self, code: int) -> None:
        self.code = code
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        raise _status_error(self.code)


@pytest.mark.asyncio
async def test_a_504_is_retried_once_then_raised_as_a_timeout(monkeypatch) -> None:
    provider = OpenAICompatibleProvider(api_key="x", base_url="https://router.example/v1")
    completions = _FailingCompletions(504)
    monkeypatch.setattr(provider._client, "chat", type("C", (), {"completions": completions})())

    with pytest.raises(ModelTimeout):
        await provider.complete(system="s", user="u")
    assert completions.calls == 2


class TimeoutOnLongText(VisionProvider):
    """Times out on any prompt longer than `limit`, reads anything shorter."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls: list[int] = []

    async def complete(self, **kwargs) -> ModelResponse:
        user = kwargs["user"]
        self.calls.append(len(user))
        if len(user) > self.limit:
            raise ModelTimeout("gateway timeout")
        people = [
            {"full_name": line.removeprefix("## Person ").strip(), "category": "resident"}
            for line in user.splitlines() if line.startswith("## Person")
        ]
        body = {"page_type": "roster", "people": people}
        return ModelResponse(text=json.dumps(body), usage=Usage(1, 1), model="fake")


@pytest.mark.asyncio
async def test_a_timed_out_read_is_split_and_nobody_is_lost() -> None:
    firsts = ["Ada", "Ben", "Cara", "Dev", "Eli", "Fay", "Gus", "Hana", "Ivan", "Jo", "Kai", "Lena"]
    lasts = ["Moss", "Nash", "Orr", "Pike", "Quinn", "Reyes", "Stone", "Tate", "Ueda", "Vance"]
    names = [f"{first} {last}" for first in firsts for last in lasts]
    text = "\n".join(f"## Person {name}\n" + "x" * 60 for name in names)
    provider = TimeoutOnLongText(limit=len(text) // 2 + 3_000)
    meter = UsageMeter()

    reading = await read_page(url="https://x.org/r", title="Residents", text=text,
                              provider=provider, meter=meter)

    assert reading.ok
    assert len(reading.people) == 120
    # The split read recovered, so it is not counted as a failed call.
    assert meter.failures == 0


class TriageTimeoutProvider(VisionProvider):
    def __init__(self, max_links: int) -> None:
        self.max_links = max_links
        self.batch_sizes: list[int] = []

    async def complete(self, **kwargs) -> ModelResponse:
        lines = [line for line in kwargs["user"].splitlines() if line[:1].isdigit()]
        self.batch_sizes.append(len(lines))
        if len(lines) > self.max_links:
            raise ModelTimeout("gateway timeout")
        body = {"links": [{"i": i, "p": 70} for i in range(len(lines))]}
        return ModelResponse(text=json.dumps(body), usage=Usage(1, 1), model="fake")


@pytest.mark.asyncio
async def test_a_timed_out_triage_batch_is_halved() -> None:
    links = [{"url": f"https://x.org/page-{i}", "text": f"Page {i}"} for i in range(40)]
    provider = TriageTimeoutProvider(max_links=25)

    decisions = await triage_links(links, source="test", provider=provider, batch_size=40)

    assert [d.by_model for d in decisions] == [True] * 40
    assert provider.batch_sizes[0] == 40 and max(provider.batch_sizes[1:]) <= 20


def test_triage_uses_the_cheap_model_when_one_is_set(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_cheap_model", "small-model")
    assert settings.cheap_model == "small-model"
    monkeypatch.setattr(settings, "llm_cheap_model", "")
    assert settings.cheap_model == settings.text_model


def test_cheap_model_calls_are_priced_at_the_cheap_rate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_cheap_model", "small-model")
    cheap = Usage(1_000_000, 0, model="small-model")
    main = Usage(1_000_000, 0, model=settings.text_model)
    assert cheap.cost_usd == pytest.approx(settings.llm_cheap_price_input_per_mtok)
    assert main.cost_usd == pytest.approx(settings.llm_price_input_per_mtok)
    assert (cheap + main).cost_usd == pytest.approx(cheap.cost_usd + main.cost_usd)
