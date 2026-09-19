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
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from ..config import settings
from .usage import Usage, UsageMeter

log = logging.getLogger("agentscrape.llm")


class ModelTimeout(RuntimeError):
    """The request took longer than the gateway or our client would wait.

    Resending the identical request times out again, and each attempt holds a
    slot of the process-wide gate for the whole window, which starved other
    calls into timeouts of their own. Callers shrink the request instead.
    """


# The Hugging Face router answers 504 when the upstream provider does not
# finish in time; 408 and 524 are the same thing from other gateways.
_TIMEOUT_STATUSES = frozenset({408, 504, 524})
# One more try for a timeout, in case it was queueing rather than size.
_TIMEOUT_ATTEMPTS = 2


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
    # Fall back to the outermost array/object, trying whichever opens first:
    # trying arrays first pulled the inner list out of {"hosts": [...]} and
    # lost the object that was actually asked for.
    pairs = sorted(
        (("[", "]"), ("{", "}")),
        key=lambda pair: (cleaned.find(pair[0]) == -1, cleaned.find(pair[0])),
    )
    for opener, closer in pairs:
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
        model: str | None = None,
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
        model: str | None = None,
    ) -> ModelResponse:
        async with _gate():
            return await self._complete(
                system=system, user=user, image_bytes=image_bytes, meter=meter,
                max_tokens=max_tokens, model=model or self.model,
            )

    async def _complete(
        self,
        *,
        system: str,
        user: str,
        image_bytes: bytes | None,
        meter: UsageMeter | None,
        max_tokens: int | None,
        model: str,
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
        timeouts = 0
        limit = max_tokens or settings.llm_max_output_tokens
        prompt_chars = len(system) + len(user)
        for attempt in range(settings.llm_max_retries):
            started = time.monotonic()
            try:
                response = await self._client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=limit,
                    temperature=0.0,  # extraction, not generation
                )
            except APITimeoutError as exc:
                timeouts += 1
                self._log_failure(exc, "timeout", model, prompt_chars, limit, started)
                if timeouts >= _TIMEOUT_ATTEMPTS:
                    raise ModelTimeout(f"model call timed out: {exc}") from exc
                continue
            except (RateLimitError, APIConnectionError) as exc:
                last_error = exc
                delay = min(2**attempt, 30)
                retry_after = _retry_after(exc)
                if retry_after is not None:
                    delay = min(max(retry_after, 1.0), 60.0)
                self._log_failure(exc, type(exc).__name__, model, prompt_chars, limit, started)
                await asyncio.sleep(delay)
                continue
            except APIStatusError as exc:
                self._log_failure(exc, exc.status_code, model, prompt_chars, limit, started)
                if exc.status_code in _TIMEOUT_STATUSES:
                    timeouts += 1
                    if timeouts >= _TIMEOUT_ATTEMPTS:
                        raise ModelTimeout(
                            f"model call timed out (HTTP {exc.status_code})"
                        ) from exc
                    continue
                if exc.status_code >= 500 and attempt < settings.llm_max_retries - 1:
                    last_error = exc
                    await asyncio.sleep(min(2**attempt, 15))
                    continue
                raise

            raw_usage = getattr(response, "usage", None)
            usage = Usage(
                input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
                model=model,
            )
            if meter is not None:
                await meter.record(usage)
            text = (response.choices[0].message.content or "") if response.choices else ""
            return ModelResponse(text=text, usage=usage, model=model)

        raise RuntimeError(
            f"model call failed after {settings.llm_max_retries} attempts: {last_error}"
        )


    @staticmethod
    def _log_failure(
        exc: Exception, what: object, model: str, prompt_chars: int, max_tokens: int,
        started: float,
    ) -> None:
        """Enough to tell a gateway timeout on an oversized request from an outage."""
        log.warning(
            "model call failed (%s) after %.0fs: model=%s prompt_chars=%d max_tokens=%d: %s",
            what, time.monotonic() - started, model, prompt_chars, max_tokens,
            str(exc)[:200],
        )


def _retry_after(exc: Exception) -> float | None:
    """Seconds the server asked us to wait, when it said."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _gate() -> asyncio.Semaphore:
    """One process-wide cap on model calls in flight, bound to the running loop."""
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(max(settings.llm_concurrency, 1))
        _semaphore_loop = loop
    return _semaphore


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
