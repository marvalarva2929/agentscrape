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
    # Staff-only areas: launching runs, spend and site stats.
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
    llm_timeout_seconds: int = 180
    llm_max_retries: int = 6
    # A 100-person roster is several thousand tokens of JSON; 4096 truncated it.
    llm_max_output_tokens: int = 16_000
    # Model calls in flight at once, across every site in the process.
    llm_concurrency: int = 24
    # This many model failures in a row abort the site. A broken endpoint used
    # to degrade silently into a heuristic-only crawl that looked like success.
    llm_max_consecutive_failures: int = 20
    # Page text is read in chunks of this many characters.
    llm_page_chunk_chars: int = 40_000

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
    discovery_timeout_seconds: int = 600
    discovery_source_timeout_seconds: int = 60
    enable_crt_sh: bool = True
    crt_sh_timeout_seconds: int = 30
    search_provider: str = "none"  # none | brave | serper
    search_api_key: str = ""

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
