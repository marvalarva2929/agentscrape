"""The per-site pipeline as a LangGraph graph.

    entry -> validate -> skip_check -> discover -> plan -> extract (loop)
          -> gap_fill -> extract (loop) ... -> finalize

Any stage can terminate the site early by setting `terminated`. The extract node
loops until the work list stops being worth working (see `_after_extract`), the
step budget is spent, or the run hits a hard stop. Before giving up, gap_fill
asks the model where the rosters of still-uncovered programs might be.

Dependencies (browser context, HTTP client, sessions) are bound by closure so
the checkpointed state stays JSON-serializable and a run can resume after the
server is stopped.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from ..config import settings
from ..orchestrator.events import RunStage
from .checkpoint import apply_checkpoint, load_checkpoint
from .deps import PipelineDeps
from .nodes.discover import discover_links
from .nodes.extract import extract_batch
from .nodes.finalize import finalize
from .nodes.plan import MAX_GAP_ROUNDS, gap_fill, pending_programs, plan_programs
from .nodes.skip_check import skip_check
from .nodes.validate import validate_institution
from .state import SiteState

log = logging.getLogger("agentscrape.pipeline")

# The work list is priority-ordered, so once its head drops below these the
# rest is not worth the time: below FLOOR always, below COVERED_FLOOR once
# every known program has its roster.
PRIORITY_FLOOR = 5.0
COVERED_FLOOR = 30.0


def build_site_graph(deps: PipelineDeps):
    """Compile the per-site graph with its runtime dependencies bound."""

    async def _entry(state: SiteState) -> SiteState:
        """Restore a mid-site checkpoint, if this site was interrupted."""
        async with deps.sessionmaker() as session:
            checkpoint = await load_checkpoint(session, state["site_run_id"])
        if checkpoint is None:
            return state
        return apply_checkpoint(state, checkpoint)

    async def _validate(state: SiteState) -> SiteState:
        return await validate_institution(state, deps)

    async def _skip(state: SiteState) -> SiteState:
        await deps.emit_stage(state, RunStage.DISCOVERING)
        return await skip_check(state, deps)

    async def _discover(state: SiteState) -> SiteState:
        return await discover_links(state, deps)

    async def _plan(state: SiteState) -> SiteState:
        return await plan_programs(state, deps)

    async def _gap_fill(state: SiteState) -> SiteState:
        return await gap_fill(state, deps)

    async def _extract(state: SiteState) -> SiteState:
        await deps.emit_stage(state, RunStage.DIRECTORY)
        return await extract_batch(state, deps)

    async def _finalize(state: SiteState) -> SiteState:
        await deps.emit_stage(state, RunStage.FINALIZING)
        return await finalize(state, deps)

    def _after_entry(state: SiteState) -> str:
        # A resumed site already has its ranked candidate list, so it skips
        # validation, the skip check and discovery and goes back to work.
        return "extract" if state.get("resumed") else "validate"

    def _after_validate(state: SiteState) -> str:
        return "finalize" if state.get("terminated") else "skip_check"

    def _after_skip(state: SiteState) -> str:
        return "finalize" if state.get("terminated") else "discover"

    def _after_discover(state: SiteState) -> str:
        return "plan" if state.get("candidates") else "finalize"

    def _worklist_done(state: SiteState) -> str | None:
        """Why the work list is no longer worth working, or None."""
        candidates = state.get("candidates", [])
        cursor = state.get("cursor", 0)
        if cursor >= len(candidates):
            return "work list exhausted"
        head = float(candidates[cursor].get("priority", 0.0))
        if head < PRIORITY_FLOOR:
            return f"best remaining link has priority {head:.0f}"
        programs = state.get("programs", [])
        if programs and not pending_programs(state) and head < COVERED_FLOOR:
            return f"every program covered and best remaining priority is {head:.0f}"
        if state.get("barren_streak", 0) >= settings.stop_after_barren_pages:
            return f"{state['barren_streak']} consecutive pages yielded nobody"
        return None

    def _after_extract(state: SiteState) -> str:
        if deps.stop_requested():
            return "finalize"
        if state.get("steps_taken", 0) >= state["step_budget"]:
            log.info("step budget exhausted for %s", state["root_domain"])
            return "finalize"
        reason = _worklist_done(state)
        if reason is None:
            return "extract"
        pending = pending_programs(state)
        if pending and state.get("gap_rounds", 0) < MAX_GAP_ROUNDS:
            log.info(
                "%s: %s; %d programs still uncovered, filling gaps",
                state["root_domain"], reason, len(pending),
            )
            return "gap_fill"
        log.info("%s: %s; finishing", state["root_domain"], reason)
        return "finalize"

    def _after_gap_fill(state: SiteState) -> str:
        return "finalize" if _worklist_done(state) == "work list exhausted" else "extract"

    graph = StateGraph(SiteState)
    graph.add_node("entry", _entry)
    graph.add_node("validate", _validate)
    graph.add_node("skip_check", _skip)
    graph.add_node("discover", _discover)
    graph.add_node("plan", _plan)
    graph.add_node("extract", _extract)
    graph.add_node("gap_fill", _gap_fill)
    graph.add_node("finalize", _finalize)

    graph.set_entry_point("entry")
    graph.add_conditional_edges("entry", _after_entry, ["validate", "extract"])
    graph.add_conditional_edges("validate", _after_validate, ["skip_check", "finalize"])
    graph.add_conditional_edges("skip_check", _after_skip, ["discover", "finalize"])
    graph.add_conditional_edges("discover", _after_discover, ["plan", "finalize"])
    graph.add_edge("plan", "extract")
    graph.add_conditional_edges("extract", _after_extract, ["extract", "gap_fill", "finalize"])
    graph.add_conditional_edges("gap_fill", _after_gap_fill, ["extract", "finalize"])
    graph.add_edge("finalize", END)

    return graph.compile()
