"""Stage 0: reject K-12 institutions before any work is done."""

from __future__ import annotations

import logging

from ...db.enums import ValidationStatus
from ...db.repositories.sites import get_site, record_validation
from ...llm.prompts import INSTITUTION_SYSTEM, institution_user_prompt
from ...llm.provider import get_provider
from ...urls import home_url, host_of
from ...validation.institution import InstitutionVerdict, classify_content, classify_domain
from ..browser_fetch import browser_fetch
from ..deps import PipelineDeps
from ..state import SiteState

log = logging.getLogger("agentscrape.pipeline.validate")


async def _model_review(deps: PipelineDeps, domain: str, title: str, text: str):
    """Only reached when hostname and content heuristics were both inconclusive."""
    try:
        response = await get_provider().complete(
            system=INSTITUTION_SYSTEM,
            user=institution_user_prompt(domain=domain, title=title, text=text),
            meter=deps.meter,
            max_tokens=200,
        )
        payload = response.json()
    except Exception as exc:
        log.warning("institution model review failed for %s: %s", domain, exc)
        return None
    if not isinstance(payload, dict):
        return None

    kind = str(payload.get("type", "")).lower()
    reason = str(payload.get("reason", "")).strip() or "model classification"
    if kind == "k12":
        return InstitutionVerdict(
            ValidationStatus.K12_REJECTED,
            f"Model classified this as a K-12 institution: {reason}",
            institution_type="k12",
        )
    if kind == "post_secondary":
        return InstitutionVerdict(
            ValidationStatus.POST_SECONDARY,
            f"Model classified this as post-secondary: {reason}",
            institution_type="university",
        )
    return InstitutionVerdict(
        ValidationStatus.UNKNOWN, f"Model could not classify this institution: {reason}"
    )


async def validate_institution(state: SiteState, deps: PipelineDeps) -> SiteState:
    """Explicit scope gate. A rejection is a stated reason, never a silent drop."""
    root_url = state["root_url"]
    domain = state["root_domain"]
    result_status: int | None = None

    verdict = classify_domain(root_url)

    if verdict is None or verdict.needs_model_review:
        result = await deps.fetcher.get(root_url)
        if not result.ok:
            # A dead entry link is not a dead site: judge the home page instead
            # (discovery falls back to it the same way).
            home = home_url(root_url)
            if home != root_url:
                retry = await deps.fetcher.get(home)
                if retry.ok:
                    result = retry
        if not result.ok and result.status != 429 and deps.can_render:
            # Turned away over plain HTTP (a refusal, a reset, a certificate the
            # script cannot check): open it the way a person would before judging
            # the site unreachable. A 429 is a request to slow down and stays one.
            for target in dict.fromkeys([root_url, home_url(root_url)]):
                attempt = await browser_fetch(deps, target)
                if attempt.ok:
                    result = attempt
                    deps.browser_only = True  # plain HTTP is refused here; read pages the same way
                    await deps.note(state, f"{target} refused plain requests but opened in a browser.")
                    break
        if not result.ok:
            result_status = result.status
            if result.status == 403:
                reason = (
                    f"{root_url} blocked automated requests (HTTP 403). "
                    "This site could not be crawled; try again later."
                )
            elif result.status == 429:
                reason = (
                    f"{root_url} rate-limited this crawl (HTTP 429). "
                    "This site could not be crawled; try again later."
                )
            else:
                reason = f"Could not load {root_url}: {result.error or 'unknown error'}"
            verdict = InstitutionVerdict(
                ValidationStatus.UNREACHABLE,
                reason,
            )
        else:
            from selectolax.parser import HTMLParser

            tree = HTMLParser(result.text)
            title_node = tree.css_first("title")
            title = title_node.text().strip() if title_node else ""
            body = tree.body.text(separator=" ")[:100_000] if tree.body else ""
            verdict = classify_content(domain, body, title)
            if verdict.needs_model_review:
                verdict = await _model_review(deps, domain, title, body) or verdict

    async with deps.sessionmaker() as session:
        site = await get_site(session, state["site_id"])
        if site is not None:
            await record_validation(
                session, site,
                status=verdict.status,
                reason=verdict.reason,
                institution_type=verdict.institution_type,
            )
        await session.commit()

    if verdict.accepted:
        log.info("site %s accepted: %s", domain, verdict.reason)
        return {**state, "status": "running"}

    code = (
        "K12_INSTITUTION_REJECTED"
        if verdict.status == ValidationStatus.K12_REJECTED
        else "SITE_BLOCKED"
        if verdict.status == ValidationStatus.UNREACHABLE and result_status == 403
        else "SITE_RATE_LIMITED"
        if verdict.status == ValidationStatus.UNREACHABLE and result_status == 429
        else "SITE_UNREACHABLE"
        if verdict.status == ValidationStatus.UNREACHABLE
        else "INSTITUTION_NOT_POST_SECONDARY"
    )
    log.info("site %s rejected (%s): %s", domain, code, verdict.reason)
    return {
        **state,
        "status": "rejected",
        "terminated": True,
        "error_code": code,
        "error_message": verdict.reason,
    }


def hostname(url: str) -> str:
    return host_of(url)
