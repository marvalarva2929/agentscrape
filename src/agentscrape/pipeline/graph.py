"""The per-site pipeline as a LangGraph graph.

    entry -> validate -> discover -> plan -> [html_map]
          -> extract (loop) -> gap_fill -> extract (loop) ... -> finalize

`plan` runs first so everything after it works program-first: the HTML map
fetches under program landing pages before anything else, and the work list
puts pages of programs still missing a roster ahead of all other pages.
`html_map` runs on the hybrid strategy only: a plain-HTML pass that maps the
site without the model, so the model reads only pages that show people.

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
from .nodes.directory import directory_search
from .nodes.discover import discover_links
from .nodes.extract import extract_batch, next_is_program_page
from .nodes.finalize import finalize
from .nodes.html_map import html_map
from .nodes.plan import MAX_GAP_ROUNDS, gap_fill, pending_programs, plan_programs
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

    async def _discover(state: SiteState) -> SiteState:
        await deps.emit_stage(state, RunStage.DISCOVERING)
        return await discover_links(state, deps)

    async def _html_map(state: SiteState) -> SiteState:
        return await html_map(state, deps)

    async def _plan(state: SiteState) -> SiteState:
        return await plan_programs(state, deps)

    async def _gap_fill(state: SiteState) -> SiteState:
        return await gap_fill(state, deps)

    async def _extract(state: SiteState) -> SiteState:
        await deps.emit_stage(state, RunStage.DIRECTORY)
        return await extract_batch(state, deps)

    async def _directory(state: SiteState) -> SiteState:
        await deps.emit_stage(state, RunStage.DIRECTORY)
        return await directory_search(state, deps)

    async def _finalize(state: SiteState) -> SiteState:
        await deps.emit_stage(state, RunStage.FINALIZING)
        return await finalize(state, deps)

    def _wants_directory(state: SiteState) -> bool:
        return "directory" in (state.get("modes") or ["crawl"])

    def _crawl_over(state: SiteState) -> str:
        """Where a site goes once its crawl is finished: the
        directory search when this run asked for one, else finalize. A site
        stopped for a reason (rejected, failed, a hard stop) goes straight
        to finalize."""
        if (
            _wants_directory(state)
            and not deps.stop_requested()
            and state.get("status") != "failed"
            and state.get("error_code") is None
        ):
            return "directory"
        return "finalize"

    def _after_entry(state: SiteState) -> str:
        if "crawl" not in (state.get("modes") or ["crawl"]) or state.get("crawl_done"):
            return "directory"
        # A resumed site already has its ranked candidate list, so it skips
        # validation and discovery and goes back to work.
        return "extract" if state.get("resumed") else "validate"

    def _after_validate(state: SiteState) -> str:
        return "finalize" if state.get("terminated") else "discover"

    def _after_discover(state: SiteState) -> str:
        if not state.get("candidates"):
            return _crawl_over(state)
        return "plan"

    def _after_plan(state: SiteState) -> str:
        return "html_map" if state.get("crawl_strategy") == "hybrid" else "extract"

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
        if state.get("terminated"):
            # Blocked or rate-limited mid-crawl (extract_batch set the reason).
            return "finalize"
        if deps.crawl_limit():
            log.info("%s: the run collected as many people as it asked for", state["root_domain"])
            return _crawl_over(state)
        if state.get("steps_taken", 0) >= state["step_budget"]:
            log.info("step budget exhausted for %s", state["root_domain"])
            return _crawl_over(state)
        reason = _worklist_done(state)
        pending = pending_programs(state)
        if reason is None:
            # No queued page belongs to a program still missing a roster:
            # ask the model where those rosters are before moving on to
            # everything else on the site.
            if (
                pending
                and not next_is_program_page(state)
                and state.get("gap_rounds", 0) < MAX_GAP_ROUNDS
            ):
                log.info(
                    "%s: no queued pages for %d programs still missing a roster; filling gaps",
                    state["root_domain"], len(pending),
                )
                return "gap_fill"
            return "extract"
        if pending and state.get("gap_rounds", 0) < MAX_GAP_ROUNDS:
            log.info(
                "%s: %s; %d programs still uncovered, filling gaps",
                state["root_domain"], reason, len(pending),
            )
            return "gap_fill"
        log.info("%s: %s; finishing", state["root_domain"], reason)
        return _crawl_over(state)

    def _after_gap_fill(state: SiteState) -> str:
        return _crawl_over(state) if _worklist_done(state) == "work list exhausted" else "extract"

    graph = StateGraph(SiteState)
    graph.add_node("entry", _entry)
    graph.add_node("validate", _validate)
    graph.add_node("discover", _discover)
    graph.add_node("html_map", _html_map)
    graph.add_node("plan", _plan)
    graph.add_node("extract", _extract)
    graph.add_node("gap_fill", _gap_fill)
    graph.add_node("directory", _directory)
    graph.add_node("finalize", _finalize)

    graph.set_entry_point("entry")
    graph.add_conditional_edges("entry", _after_entry, ["validate", "extract", "directory"])
    graph.add_conditional_edges("validate", _after_validate, ["discover", "finalize"])
    graph.add_conditional_edges("discover", _after_discover, ["plan", "directory", "finalize"])
    graph.add_conditional_edges("plan", _after_plan, ["html_map", "extract"])
    graph.add_edge("html_map", "extract")
    graph.add_conditional_edges(
        "extract", _after_extract, ["extract", "gap_fill", "directory", "finalize"]
    )
    graph.add_conditional_edges("gap_fill", _after_gap_fill, ["extract", "directory", "finalize"])
    graph.add_edge("directory", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()
