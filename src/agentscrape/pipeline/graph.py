"""The per-site pipeline as a LangGraph graph.

    entry -> validate -> skip_check -> discover -> extract (loop) -> finalize

Any stage can terminate the site early by setting `terminated`. The extract node
loops until the candidate list is exhausted, the step budget is spent, or the run
hits a hard stop.

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
from .nodes.skip_check import skip_check
from .nodes.validate import validate_institution
from .state import SiteState

log = logging.getLogger("agentscrape.pipeline")


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
        return "extract" if state.get("candidates") else "finalize"

    def _after_extract(state: SiteState) -> str:
        """Keep working the list until budget, candidates or the run runs out."""
        if deps.stop_requested():
            return "finalize"
        if state.get("steps_taken", 0) >= state["step_budget"]:
            log.info("step budget exhausted for %s", state["root_domain"])
            return "finalize"
        if state.get("cursor", 0) >= len(state.get("candidates", [])):
            return "finalize"
        barren = state.get("barren_streak", 0)
        if barren >= settings.stop_after_barren_pages:
            # The site has stopped yielding new people. Ranked candidates mean
            # the remaining ones are the least promising, so keep the budget.
            log.info(
                "%s: %d consecutive pages with nobody new; stopping",
                state["root_domain"], barren,
            )
            return "finalize"
        return "extract"

    graph = StateGraph(SiteState)
    graph.add_node("entry", _entry)
    graph.add_node("validate", _validate)
    graph.add_node("skip_check", _skip)
    graph.add_node("discover", _discover)
    graph.add_node("extract", _extract)
    graph.add_node("finalize", _finalize)

    graph.set_entry_point("entry")
    graph.add_conditional_edges("entry", _after_entry, ["validate", "extract"])
    graph.add_conditional_edges("validate", _after_validate, ["skip_check", "finalize"])
    graph.add_conditional_edges("skip_check", _after_skip, ["discover", "finalize"])
    graph.add_conditional_edges("discover", _after_discover, ["extract", "finalize"])
    graph.add_conditional_edges("extract", _after_extract, ["extract", "finalize"])
    graph.add_edge("finalize", END)

    return graph.compile()
