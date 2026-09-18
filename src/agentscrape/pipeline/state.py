"""Per-site pipeline state.

Everything here must be JSON-serializable: LangGraph checkpoints it so a run can
resume after the server is stopped mid-flight.
"""

from __future__ import annotations

from typing import Any, TypedDict

from ..urls import registrable_domain


class SiteState(TypedDict, total=False):
    # --- identity ---
    site_id: str
    site_run_id: str
    run_id: str | None
    root_url: str
    root_domain: str
    # Every registrable domain this institution publishes on. A medical centre
    # routinely spans two: the university and the health system it staffs.
    allowed_domains: list[str]
    agent_id: str

    # --- configuration for this site ---
    force_rescan: bool
    skip_threshold: float
    step_budget: int

    # --- control flow ---
    status: str
    terminated: bool
    resumed: bool
    skip_reason: str | None
    similarity_score: float | None
    error_code: str | None
    error_message: str | None

    # --- work list ---
    # {url, score (heuristic), priority (model), program, is_known_path}
    candidates: list[dict[str, Any]]
    cursor: int
    steps_taken: int
    candidates_considered: int
    known_path_hits: int
    # Consecutive candidate pages that produced nobody at all. Counting
    # "nobody new" instead would mark every page of an unchanged site barren.
    barren_streak: int
    # Every link the model has already been asked about, by case-folded hash,
    # so site-wide navigation is triaged once rather than on every page.
    triaged: list[str]
    # Content hashes of pages already read; a duplicate body is not re-read.
    processed_hashes: list[str]
    # The institution's programs and whether each has a roster yet:
    # {name, kind, landing_url, status, people, pages, visited}
    programs: list[dict[str, Any]]
    gap_rounds: int

    # --- results ---
    records_new: int
    records_changed: int
    records_unchanged: int
    records_missing: int
    seen_record_ids: list[str]
    fingerprint: dict[str, str]
    dominant_specialty: str | None
    tokens_in: int
    tokens_out: int
    spend_usd: float


def initial_state(
    *,
    site_id: str,
    site_run_id: str,
    root_url: str,
    root_domain: str,
    allowed_domains: list[str] | None = None,
    run_id: str | None = None,
    agent_id: str = "agent-0",
    force_rescan: bool = False,
    skip_threshold: float = 0.90,
    step_budget: int = 40,
) -> SiteState:
    return SiteState(
        site_id=site_id,
        site_run_id=site_run_id,
        run_id=run_id,
        root_url=root_url,
        root_domain=root_domain,
        allowed_domains=sorted(
            {registrable_domain(root_domain), *(allowed_domains or [])}
        ),
        agent_id=agent_id,
        force_rescan=force_rescan,
        skip_threshold=skip_threshold,
        step_budget=step_budget,
        status="running",
        terminated=False,
        resumed=False,
        skip_reason=None,
        similarity_score=None,
        error_code=None,
        error_message=None,
        candidates=[],
        cursor=0,
        steps_taken=0,
        candidates_considered=0,
        known_path_hits=0,
        barren_streak=0,
        triaged=[],
        processed_hashes=[],
        programs=[],
        gap_rounds=0,
        records_new=0,
        records_changed=0,
        records_unchanged=0,
        records_missing=0,
        seen_record_ids=[],
        fingerprint={},
        dominant_specialty=None,
        tokens_in=0,
        tokens_out=0,
        spend_usd=0.0,
    )
