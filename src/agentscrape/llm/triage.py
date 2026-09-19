"""The model decides which links and hosts are worth visiting.

This replaces keyword score floors (`FRONTIER_MIN_SCORE`, `min_score=1.0`) and
hostname deny-lists as the gate. A link is dropped only when the model says
"skip"; anything it does not mention, and everything in a batch whose call
fails, is kept at a priority derived from the heuristic score. Recall comes
first: an unneeded fetch costs seconds, a missed roster costs the client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from ..config import settings
from ..discovery.scoring import score_url
from .prompts import HOST_TRIAGE_SYSTEM, TRIAGE_SYSTEM, triage_user_prompt
from .provider import ModelTimeout, VisionProvider, get_provider
from .usage import LLMUnavailable, UsageMeter

log = logging.getLogger("agentscrape.llm.triage")

# Smaller batches keep each answer short enough to finish inside the
# router's gateway window.
LINK_BATCH = 60
URL_ONLY_BATCH = 120
HOST_BATCH = 150
# One decision object, tolerant of what breaks strict JSON in long outputs: an
# unquoted skip, a truncated tail, stray prose between items.
_ENTRY = re.compile(
    r'\{\s*"i"\s*:\s*(\d+)\s*,\s*"p"\s*:\s*("?skip"?|\d+(?:\.\d+)?)'
    r'(?:\s*,\s*"program"\s*:\s*("(?:[^"\\]|\\.)*"|null))?\s*\}',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LinkDecision:
    url: str
    priority: float  # 0 means skip
    program: str | None = None
    by_model: bool = False
    heuristic: float = 0.0

    @property
    def skipped(self) -> bool:
        return self.priority <= 0


def heuristic_priority(score: float) -> float:
    """Fallback ordering when the model is not consulted: keep everything, rank
    by the old keyword score, and never outrank a model-assigned roster."""
    return max(5.0, min(60.0, 25.0 + score * 2.5))


def salvage_links(text: str) -> dict | None:
    """Recover every well-formed decision from output that is not valid JSON."""
    items = []
    for index, priority, program in _ENTRY.findall(text or ""):
        entry: dict = {"i": int(index)}
        entry["p"] = "skip" if "skip" in priority.lower() else float(priority)
        if program and program != "null":
            try:
                entry["program"] = json.loads(program)
            except json.JSONDecodeError:
                pass
        items.append(entry)
    return {"links": items} if items else None


async def _call(
    provider: VisionProvider, system: str, user: str, meter: UsageMeter | None, what: str,
    *, raise_timeout: bool = False,
) -> dict | None:
    """One triage call. With `raise_timeout`, a timeout propagates so the caller
    can retry with a smaller batch instead of losing the whole batch."""
    for attempt in range(2):
        try:
            response = await provider.complete(
                system=system, user=user, meter=meter, model=settings.cheap_model,
                max_tokens=12_000,
            )
        except LLMUnavailable:
            raise
        except ModelTimeout:
            if raise_timeout:
                raise
            if meter is not None:
                meter.note_failure(what, "timeout")
            return None
        except Exception as exc:
            log.warning("%s failed: %s", what, exc)
            if meter is not None:
                meter.note_failure(what, exc)
            return None
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        salvaged = salvage_links(response.text) if system is TRIAGE_SYSTEM else None
        if salvaged:
            log.info("%s: recovered %d decisions from malformed output", what, len(salvaged["links"]))
            return salvaged
        log.warning(
            "%s returned unparseable output (attempt %d): %r", what, attempt + 1,
            (response.text or "")[-200:],
        )
    if meter is not None:
        meter.note_failure(what, "unparseable output")
    return None


async def triage_links(
    links: list[dict],
    *,
    source: str,
    context: str = "",
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
    batch_size: int | None = None,
) -> list[LinkDecision]:
    """Decide on each link. `links` are dicts with `url` and optionally `text`,
    `heading`, `in_nav`. Returns one decision per input link, in order."""
    if not links:
        return []
    provider = provider or get_provider()
    size = batch_size or (LINK_BATCH if any(link.get("text") for link in links) else URL_ONLY_BATCH)
    heuristics = [score_url(link["url"], title=link.get("text") or None).score for link in links]

    async def run(start: int, count: int, splits_left: int = 1) -> list[LinkDecision]:
        batch = links[start : start + count]
        try:
            payload = await _call(
                provider, TRIAGE_SYSTEM,
                triage_user_prompt(source=source, context=context, links=batch),
                meter, f"triage ({source})", raise_timeout=splits_left > 0 and len(batch) > 1,
            )
        except ModelTimeout:
            half = (len(batch) + 1) // 2
            log.info("triage of %d links timed out; retrying as two batches", len(batch))
            first, second = await asyncio.gather(
                run(start, half, splits_left - 1),
                run(start + half, len(batch) - half, splits_left - 1),
            )
            return [*first, *second]
        verdicts: dict[int, tuple[float, str | None]] = {}
        if payload and isinstance(payload.get("links"), list):
            for item in payload["links"]:
                if not isinstance(item, dict):
                    continue
                index = item.get("i")
                if not isinstance(index, int) or not 0 <= index < len(batch):
                    continue
                raw = item.get("p")
                if isinstance(raw, str) and raw.strip().lower() == "skip":
                    priority = 0.0
                elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    priority = float(max(1, min(100, raw)))
                else:
                    continue
                program = item.get("program")
                verdicts[index] = (
                    priority, program.strip()[:200] if isinstance(program, str) and program.strip() else None
                )
        out = []
        for offset, link in enumerate(batch):
            h = heuristics[start + offset]
            if offset in verdicts:
                priority, program = verdicts[offset]
                out.append(LinkDecision(link["url"], priority, program, True, h))
            else:
                out.append(LinkDecision(link["url"], heuristic_priority(h), None, False, h))
        return out

    results = await asyncio.gather(*(
        run(i, min(size, len(links) - i)) for i in range(0, len(links), size)
    ))
    decisions = [d for group in results for d in group]
    kept = sum(1 for d in decisions if not d.skipped)
    log.info(
        "triage (%s): %d links, %d kept, %d skipped, %d decided by heuristic fallback",
        source, len(decisions), kept, len(decisions) - kept,
        sum(1 for d in decisions if not d.by_model),
    )
    return decisions


async def triage_hosts(
    host_samples: dict[str, list[str]],
    *,
    root_domain: str,
    meter: UsageMeter | None = None,
    provider: VisionProvider | None = None,
) -> set[str]:
    """Hosts the model says to skip. Everything else, and everything in a failed
    batch, is kept."""
    if not host_samples:
        return set()
    provider = provider or get_provider()
    hosts = sorted(host_samples)

    async def run(start: int) -> set[str]:
        batch = hosts[start : start + HOST_BATCH]
        lines = [
            f"{i}. {host}  e.g. {', '.join(host_samples[host][:5]) or '/'}"
            for i, host in enumerate(batch)
        ]
        payload = await _call(
            provider, HOST_TRIAGE_SYSTEM,
            f"Institution entry point: {root_domain}\n\nHosts:\n" + "\n".join(lines),
            meter, "host triage",
        )
        skipped: set[str] = set()
        if payload and isinstance(payload.get("hosts"), list):
            for item in payload["hosts"]:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("i"), int)
                    and 0 <= item["i"] < len(batch)
                    and item.get("keep") is False
                ):
                    skipped.add(batch[item["i"]])
        return skipped

    groups = await asyncio.gather(*(run(i) for i in range(0, len(hosts), HOST_BATCH)))
    skipped = set().union(*groups)
    log.info("host triage for %s: %d hosts, %d skipped", root_domain, len(hosts), len(skipped))
    return skipped
