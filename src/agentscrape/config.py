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
    token_ttl_hours: int = 720

    # --- database -------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://agentscrape:agentscrape@localhost:5433/agentscrape"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # --- model provider (OpenAI-compatible) -----------------------------
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "Qwen/Qwen2.5-VL-72B-Instruct"
    llm_price_input_per_mtok: float = 0.30
    llm_price_output_per_mtok: float = 0.90
    llm_timeout_seconds: int = 120
    llm_max_retries: int = 3
    llm_max_output_tokens: int = 4096

    # --- crawling -------------------------------------------------------
    user_agent: str = "agentscrape/0.1 (+contact: ops@example.com)"
    respect_robots: bool = False
    requests_per_second_per_domain: float = 2.0
    in_site_fetch_concurrency: int = 4
    page_timeout_seconds: int = 30
    fetch_timeout_seconds: int = 20
    max_html_bytes: int = 4_000_000

    # --- discovery ------------------------------------------------------
    max_candidates: int = 150
    discovery_timeout_seconds: int = 180
    discovery_source_timeout_seconds: int = 60
    enable_crt_sh: bool = True
    crt_sh_timeout_seconds: int = 30
    search_provider: str = "none"  # none | brave | serper
    search_api_key: str = ""

    # --- run defaults ---------------------------------------------------
    default_concurrency: int = 4
    max_concurrency: int = 8
    default_skip_threshold: float = 0.90
    default_step_budget: int = 40
    run_timeout_seconds: int = 86_400
    site_timeout_seconds: int = 1_800
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
