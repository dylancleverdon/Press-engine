"""Infrastructure configuration: secrets, API keys, paths.

Everything that D edits day to day (band profile, pitch template, sending hours,
caps) lives in the database instead; see ``appsettings.py``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["ollama", "openai", "anthropic", "none"]


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRESS_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./data")
    database_url: str | None = None
    base_url: str = "http://localhost:8000"

    # Discovery sources
    lastfm_api_key: str | None = None
    search_provider: Literal["brave", "serpapi", "none"] = "brave"
    brave_api_key: str | None = None
    serpapi_api_key: str | None = None
    youtube_api_key: str | None = None
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None

    # Local extraction model (spec §4.5)
    extract_llm_provider: LLMProvider = "ollama"
    extract_llm_model: str = "qwen2.5:3b"
    extract_llm_base_url: str = "http://localhost:11434"
    extract_llm_api_key: str | None = None
    extract_llm_num_ctx: int = 8192

    # Larger model for scoring / translation / reply drafts (spec §4.7)
    score_llm_provider: LLMProvider = "none"
    score_llm_model: str | None = None
    score_llm_base_url: str | None = None
    score_llm_api_key: str | None = None
    score_llm_effort: Literal["low", "medium", "high", ""] = ""

    # Google OAuth (Gmail + Calendar)
    google_client_secrets_file: Path | None = None
    google_token_file: Path | None = None

    # Fetching
    user_agent: str = "PressEngine/0.1 (personal music-press research tool)"
    http_timeout_seconds: float = 20.0
    respect_robots: bool = True
    per_host_delay_seconds: float = 2.0
    max_page_bytes: int = 3_000_000

    scheduler_enabled: bool = True
    log_level: str = "INFO"

    # Expo push service endpoint (overridable for tests)
    expo_push_url: str = "https://exp.host/--/api/v2/push/send"

    extra_cors_origins: list[str] = Field(default_factory=list)

    # Set by the Mac app when it launches the backend (desktop/src-tauri/src/backend.rs).
    desktop: bool = False
    desktop_version: str | None = None
    desktop_login_secret: str | None = None
    desktop_move_hint: bool = False  # running from the DMG: suggest moving it to Applications
    app_support_dir: Path | None = None  # folder the Mac app manages: venvs/, python/, desktop.json
    uv_path: Path | None = None
    port: int = 8000

    # Automatic updates (Mac app only): new versions are published as GitHub releases.
    update_repo: str = "dylancleverdon/Press-engine"
    update_check_hours: float = 6
    github_api: str = "https://api.github.com"

    @property
    def env_path(self) -> Path:
        """API keys entered on the Connections page are saved here."""
        return self.data_dir / ".env"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'press_engine.db').as_posix()}"

    @property
    def client_secrets_path(self) -> Path:
        return self.google_client_secrets_file or self.data_dir / "google_client_secret.json"

    @property
    def token_path(self) -> Path:
        return self.google_token_file or self.data_dir / "google_token.json"


def load_config() -> Config:
    """Read env vars, ./.env and <data dir>/.env (the last one wins over ./.env;
    real environment variables win over both)."""
    data_dir = Path(os.environ.get("PRESS_DATA_DIR", "./data"))
    cfg = Config(_env_file=(Path(".env"), data_dir / ".env"))
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@lru_cache
def get_config() -> Config:
    return load_config()
