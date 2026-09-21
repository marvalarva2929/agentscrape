"""Central configuration. Every threshold, limit and retention window lives here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- auth -----------------------------------------------------------
    app_password: str = "change-me"
    # Signs in with the "admin" scope. Clients can browse, export and start,
    # cancel or retry runs themselves, so the scope currently gates nothing extra.
    admin_password: str = "change-me-admin"
    token_ttl_hours: int = 720
    # Signed screenshot links, so an <img> tag can load one without a header.
    artifact_link_ttl_seconds: int = 3600
    # Explicit origins: the frontend is served from GitHub Pages, a different
    # origin from the API, so "*" is both unsafe and insufficient here.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- demo -----------------------------------------------------------
    # A root domain listed first in /schools, so the UI opens on it.
    featured_school: str = ""
    # Load the crawl snapshots in demo/ when the API starts on an empty
    # database, so a local checkout never opens on an empty school list. Off by
    # default: a production database that is empty — a new region, a restore
    # still in progress — should stay empty rather than quietly fill with demo
    # schools that read as real ones. `.env.example` turns it on for local work.
    seed_demo_on_startup: bool = False
    # Pick up runs that were in flight when the API last stopped. Each school
    # resumes from its checkpoint (after mapping, once per batch of pages).
    resume_runs_on_startup: bool = True
    # Where those snapshots live; blank means the repository's demo/ folder.
    demo_snapshot_dir: str = ""
    # The school spreadsheets staff are sent (CSV or .xlsx, one row per
    # institution), loaded into the school list on startup. Blank means the
    # repository's schools/ folder.
    school_sheets_dir: str = ""

    # --- database -------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://agentscrape:agentscrape@localhost:5433/agentscrape"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # --- model provider (OpenAI-compatible) -----------------------------
    llm_base_url: str = "https://router.huggingface.co/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "Qwen/Qwen3-VL-235B-A22B-Instruct"
    llm_price_input_per_mtok: float = 0.30
    llm_price_output_per_mtok: float = 0.90
    # Text-only calls (page reading, link triage, planning) can use a stronger
    # or longer-context model than the vision one. Blank means `llm_model`.
    llm_text_model: str = ""
    # High-volume, low-stakes calls (link and host triage) can run on a
    # cheaper model; both have a heuristic fallback. Blank means `text_model`.
    llm_cheap_model: str = ""
    llm_cheap_price_input_per_mtok: float = 0.10
    llm_cheap_price_output_per_mtok: float = 0.30
    llm_timeout_seconds: int = 180
    llm_max_retries: int = 6
    # A 100-person roster is several thousand tokens of JSON; 4096 truncated it.
    llm_max_output_tokens: int = 16_000
    # Model calls in flight at once, across every site in the process.
    # Kept modest: every call in flight queues at the provider, and a queued
    # long roster read is what the router's gateway answers with a 504.
    llm_concurrency: int = 8
    # This many model failures in a row abort the site. A broken endpoint used
    # to degrade silently into a heuristic-only crawl that looked like success.
    llm_max_consecutive_failures: int = 20
    # Page text is read in chunks of this many characters.
    # 40,000 produced reads long enough to hit the router's gateway timeout.
    llm_page_chunk_chars: int = 20_000

    # --- crawling -------------------------------------------------------
    user_agent: str = "agentscrape/0.1 (+contact: ops@example.com)"
    respect_robots: bool = False
    requests_per_second_per_domain: float = 2.0
    in_site_fetch_concurrency: int = 4
    page_timeout_seconds: int = 30
    fetch_timeout_seconds: int = 20
    max_html_bytes: int = 4_000_000

    # --- discovery ------------------------------------------------------
    # Only a memory bound now: the model, not a count, decides what is worth
    # visiting.
    max_candidates: int = 20_000
    # Discovery only has to find where the programs are; the planner and the
    # crawl's own link-following find the rest. 600 held a school for up to ten
    # minutes before the model read a single page.
    discovery_timeout_seconds: int = 180
    discovery_source_timeout_seconds: int = 60
    # Ranking the discovered links with the model, which decides what the
    # crawl reads first. It is not covered by the discovery budget above, and
    # when the router was slow it ran for 19 minutes of a 30-minute crawl;
    # links left unranked when it expires keep their keyword ranking.
    link_rank_timeout_seconds: int = 240
    enable_crt_sh: bool = True
    crt_sh_timeout_seconds: int = 30
    search_provider: str = "none"  # none | brave | serper
    search_api_key: str = ""

    # --- crawl strategy -------------------------------------------------
    # agent: the model ranks every discovered link and reads every page it
    # visits. hybrid: a plain-HTML pass maps the site with no model calls, and
    # the model reads only pages that show signs of people. Measured on
    # UChicago over 30 minutes (2026-09-19): agent 73.4% of the client sheet
    # for $0.98, hybrid 18.2% for $0.73 - ranking every link with the model is
    # what finds the rosters, and it is a small share of the cost.
    crawl_strategy: str = "agent"
    # Pages the HTML pass fetches before handing over to the model, and how
    # long it may take. Unmapped pages are still reachable afterwards.
    # The map runs program pages first and only orders the work list; pages
    # it does not reach are still checked for people when the crawl gets to
    # them. 1,500 pages took a quarter of an hour at a polite 2 requests a
    # second before the model read anything.
    html_map_max_pages: int = 400
    html_map_timeout_seconds: int = 120
    # Bodies kept in memory from the HTML pass so the model phase does not
    # fetch them again. Lost on resume, which only costs a re-fetch.
    html_map_cache_mb: int = 200

    # --- directory search -------------------------------------------------
    # What a run does when the request does not say: "crawl", or
    # "crawl,directory" to look everyone up in the school's directory too.
    default_run_modes: str = "crawl"
    directory_max_lookups: int = 2_000
    # Each lookup through a browser takes seconds, not milliseconds.
    directory_max_browser_lookups: int = 300

    # --- run defaults ---------------------------------------------------
    default_concurrency: int = 4
    max_concurrency: int = 8
    default_skip_threshold: float = 0.90
    # A teaching hospital publishes one roster per programme, and a medical
    # school runs 40-60 programmes across as many departmental hosts. At 40 the
    # budget was spent inside the first four departments, which is what capped a
    # full-university scrape at roughly half its rosters. Pages are plain HTTP
    # fetches at a few per second, so the budget is bounded by politeness rather
    # than cost.
    # Measured, not guessed: across five benchmarked institutions every run
    # spent its budget with 91-99% of the pages it had visited still yielding
    # people, so the crawl was being cut off mid-harvest every time.
    default_step_budget: int = 5_000
    # Stop a site once this many consecutive candidate pages yield nobody at all.
    # Candidates are ranked, so a long barren stretch means the good pages are
    # behind us. Replaces a fixed people-goal: the run ends when the site stops
    # giving, not at an arbitrary count. It has to be well above the length of
    # one department's run of brochure pages, or the crawl stops between two
    # departments that both have rosters.
    stop_after_barren_pages: int = 150
    run_timeout_seconds: int = 86_400
    # A large institution at the raised step budget runs for well over half
    # an hour, and longer again when pages have to be rendered.
    site_timeout_seconds: int = 14_400
    max_concurrent_contexts: int = 8
    estimated_mb_per_context: int = 350
    memory_safety_factor: float = 0.75

    # --- retention (None = keep forever) --------------------------------
    artifact_dir: Path = Path("./artifacts")
    screenshot_retention_days: int | None = None
    version_retention_days: int | None = None
    export_retention_days: int | None = 7

    @field_validator(
        "screenshot_retention_days",
        "version_retention_days",
        "export_retention_days",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        """An unset retention var in .env arrives as "" — that means "keep forever"."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @property
    def text_model(self) -> str:
        return self.llm_text_model or self.llm_model

    @property
    def run_modes(self) -> list[str]:
        modes = [m.strip() for m in self.default_run_modes.split(",") if m.strip()]
        return [m for m in modes if m in ("crawl", "directory")] or ["crawl"]

    @property
    def cheap_model(self) -> str:
        return self.llm_cheap_model or self.text_model

    @property
    def screenshot_dir(self) -> Path:
        return self.artifact_dir / "screenshots"

    @property
    def export_dir(self) -> Path:
        return self.artifact_dir / "exports"

    def ensure_dirs(self) -> None:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
