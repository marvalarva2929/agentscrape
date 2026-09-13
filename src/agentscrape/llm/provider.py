"""Vision-language model access behind a provider abstraction.

The specific model is a config value, not a code change: any OpenAI-compatible
endpoint works, and swapping Qwen for something else is an env edit.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError

from ..config import settings
from .usage import Usage, UsageMeter

log = logging.getLogger("agentscrape.llm")


@dataclass
class ModelResponse:
    text: str
    usage: Usage
    model: str

    def json(self) -> Any:
        """Parse a JSON body out of the response, tolerating prose and fences."""
        return parse_json_response(self.text)


def parse_json_response(text: str) -> Any:
    """Models wrap JSON in prose or ```json fences often enough to handle here."""
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost balanced array/object.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    log.warning("could not parse JSON from model response (%d chars)", len(text))
    return None


class VisionProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        user: str,
        image_bytes: bytes | None = None,
        meter: UsageMeter | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse: ...


class OpenAICompatibleProvider(VisionProvider):
    """Any OpenAI-compatible /v1/chat/completions endpoint (vLLM, TGI, Ollama…)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.model = model or settings.llm_model
        self._client = AsyncOpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key=api_key or settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # retried here so backoff and logging stay in one place
        )

    async def complete(
        self,
        *,
        system: str,
        user: str,
        image_bytes: bytes | None = None,
        meter: UsageMeter | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image_bytes:
            encoded = base64.b64encode(image_bytes).decode()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]

        last_error: Exception | None = None
        for attempt in range(settings.llm_max_retries):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=max_tokens or settings.llm_max_output_tokens,
                    temperature=0.0,  # extraction, not generation
                )
            except (RateLimitError, APIConnectionError) as exc:
                last_error = exc
                delay = min(2**attempt, 15)
                log.warning(
                    "model call failed (%s), retrying in %ss", type(exc).__name__, delay
                )
                await asyncio.sleep(delay)
                continue
            except APIStatusError as exc:
                if exc.status_code >= 500 and attempt < settings.llm_max_retries - 1:
                    last_error = exc
                    await asyncio.sleep(min(2**attempt, 15))
                    continue
                raise

            raw_usage = getattr(response, "usage", None)
            usage = Usage(
                input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
            )
            if meter is not None:
                await meter.record(usage)
            text = (response.choices[0].message.content or "") if response.choices else ""
            return ModelResponse(text=text, usage=usage, model=self.model)

        raise RuntimeError(
            f"model call failed after {settings.llm_max_retries} attempts: {last_error}"
        )


_provider: VisionProvider | None = None


def get_provider() -> VisionProvider:
    global _provider
    if _provider is None:
        _provider = OpenAICompatibleProvider()
    return _provider


def set_provider(provider: VisionProvider) -> None:
    """Injection point for tests and for swapping providers at runtime."""
    global _provider
    _provider = provider
