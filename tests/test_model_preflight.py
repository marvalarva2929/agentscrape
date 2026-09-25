"""Pre-flight model validation (check_model_configured).

A permanent provider rejection (model_not_supported) must hard-stop before
any page-level work, name the bad setting, never be confused with a
transient failure, and never re-probe the same model twice - the exact
failure mode that let one bad LLM_TEXT_MODEL value produce 20 doomed calls.
"""

from __future__ import annotations

import re

import httpx
import pytest
from openai import APIStatusError

from agentscrape.llm import provider as provider_module
from agentscrape.llm.provider import (
    ModelConfigError,
    ModelResponse,
    VisionProvider,
    check_model_configured,
)
from agentscrape.llm.usage import Usage


def _status_error(status_code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return APIStatusError("rejected", response=response, body={"error": "nope"})


class _FailingProvider(VisionProvider):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def complete(self, **kwargs) -> ModelResponse:
        self.calls += 1
        raise self.exc


class _OkProvider(VisionProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text='{"ok": true}', usage=Usage(1, 1), model="fake")


@pytest.fixture(autouse=True)
def _reset_cache():
    provider_module._model_checks.clear()
    yield
    provider_module._model_checks.clear()


@pytest.mark.asyncio
async def test_unsupported_model_is_a_permanent_config_error(monkeypatch):
    from agentscrape.config import settings

    provider = _FailingProvider(_status_error(400))
    monkeypatch.setattr(provider_module, "_provider", provider)
    monkeypatch.setattr(settings, "llm_text_model", "Qwen/Qwen2.5-VL-72B-Instruct")

    with pytest.raises(ModelConfigError, match=re.escape("Qwen/Qwen2.5-VL-72B-Instruct")):
        await check_model_configured()
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_repeated_calls_never_reprobe_a_known_bad_model(monkeypatch):
    """The exact bug being fixed: 20 separate calls to an already-known-bad
    model. Once classified, every later call raises from cache instead of
    hitting the provider again."""
    from agentscrape.config import settings

    provider = _FailingProvider(_status_error(404))
    monkeypatch.setattr(provider_module, "_provider", provider)
    monkeypatch.setattr(settings, "llm_text_model", "Qwen/Qwen2.5-VL-72B-Instruct")

    for _ in range(20):
        with pytest.raises(ModelConfigError):
            await check_model_configured()
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [500, 502, 503, 408, 504, 524])
async def test_transient_statuses_are_not_config_errors(monkeypatch, status_code):
    provider = _FailingProvider(_status_error(status_code))
    monkeypatch.setattr(provider_module, "_provider", provider)

    await check_model_configured()  # must not raise
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_other_transient_failures_are_not_config_errors(monkeypatch):
    provider = _FailingProvider(ConnectionError("network blip"))
    monkeypatch.setattr(provider_module, "_provider", provider)

    await check_model_configured()  # must not raise


@pytest.mark.asyncio
async def test_successful_check_is_cached(monkeypatch):
    provider = _OkProvider()
    monkeypatch.setattr(provider_module, "_provider", provider)

    await check_model_configured()
    await check_model_configured()
    await check_model_configured()
    assert provider.calls == 1
