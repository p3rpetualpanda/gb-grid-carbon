"""Runtime settings, read from environment variables (or a local .env file)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_target: Literal["local", "motherduck"] = "local"
    duckdb_path: Path = Path("data/gridcarbon.duckdb")
    motherduck_token: SecretStr | None = None
    motherduck_database: str = "gridcarbon"

    log_level: str = "INFO"
    # Re-load this many days on every incremental run: `actual` intensity arrives late.
    lookback_days: int = 3
    http_timeout_s: float = 60.0
    # Politeness pause between API calls (free, unauthenticated APIs).
    request_pause_s: float = 0.3


def get_settings() -> Settings:
    return Settings()
