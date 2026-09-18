"""Stage 1: skip a site whose roster has not changed.

Cheapest possible probe: re-fetch the known-good paths only. If their content
hashes are unchanged we skip without parsing; otherwise we compare the addresses
on those pages against the site's stored identity keys.
"""

from __future__ import annotations

import logging

from ...db.repositories.records import stored_identity_keys
from ...db.repositories.sites import active_known_paths, get_site
from ...domain.matching import build_identity
from ...extraction.html_people import extract_people
from ...orchestrator.events import EventType
from ..deps import PipelineDeps
from ..fingerprint import decide_skip
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.skip")

# Probing more than a handful of pages defeats the purpose of a cheap check.
MAX_PROBE_PATHS = 8


async def skip_check(state: SiteState, deps: PipelineDeps) -> SiteState:
    site_id = state["site_id"]

    async with deps.sessionmaker() as session:
        site = await get_site(session, site_id)
        previous_fingerprint = dict(site.last_fingerprint or {}) if site else {}
        stored = await stored_identity_keys(session, site_id)
        paths = await active_known_paths(session, site_id, limit=MAX_PROBE_PATHS)
        probe_urls = [p.url for p in paths]

    if state.get("force_rescan") or not stored or not probe_urls:
        decision = decide_skip(
            stored_identities=stored, probe_identities=set(),
            previous_fingerprint=previous_fingerprint, current_fingerprint={},
            threshold=state["skip_threshold"],
            force_rescan=bool(state.get("force_rescan")),
            probe_succeeded=bool(probe_urls),
        )
        log.info("site %s not skipped: %s", state["root_domain"], decision.detail)
        return {
            **state,
            "similarity_score": decision.similarity,
            "fingerprint": {},
        }

    results = await deps.fetcher.get_many(probe_urls)
    current_fingerprint: dict[str, str] = {}
    probe_identities: set[str] = set()
    any_ok = False

    for result in results:
        if not result.ok or not result.is_html:
            continue
        any_ok = True
        current_fingerprint[result.url] = result.content_hash
        for person in extract_people(result.text, url=result.url):
            identity = build_identity(
                email=person.email, full_name=person.full_name
            )
            if identity:
                probe_identities.add(identity.key)

    decision = decide_skip(
        stored_identities=stored,
        probe_identities=probe_identities,
        previous_fingerprint=previous_fingerprint,
        current_fingerprint=current_fingerprint,
        threshold=state["skip_threshold"],
        force_rescan=False,
        probe_succeeded=any_ok,
    )

    log.info(
        "skip check for %s: skip=%s (%s)",
        state["root_domain"], decision.should_skip, decision.detail,
    )

    if decision.should_skip:
        await deps.emitter.emit(
            EventType.SITE_SKIPPED,
            site_id=site_id,
            site_run_id=state["site_run_id"],
            domain=state["root_domain"],
            similarity_score=decision.similarity,
            skip_reason=decision.reason_value,
            detail=decision.detail,
        )
        return {
            **state,
            "status": "skipped",
            "terminated": True,
            "skip_reason": decision.reason_value,
            "similarity_score": decision.similarity,
            "fingerprint": current_fingerprint,
        }

    return {
        **state,
        "similarity_score": decision.similarity,
        "fingerprint": current_fingerprint,
    }
