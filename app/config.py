"""Central configuration.

Every model choice, API key, and feature flag flows through here so the brain
(Phase 2), STT, and TTS are swappable without touching call sites. Nothing in the
codebase should hardcode a model name or key — import `settings` instead.

Values are read from environment variables (prefix ``AMBER_``) and an optional
`.env` file. See `.env.example` for the full list.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AMBER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Secrets ---
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key used for both STT (Whisper) and TTS.",
    )

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # --- Models (swappable) ---
    stt_model: str = "whisper-1"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_format: str = "mp3"
    # The brain. Unused until Phase 2 but kept here so it's swappable from day one.
    llm_model: str = "claude-haiku-4-5-20251001"

    # --- Feature flags ---
    feature_stt: bool = True

    # --- Auth (Phase 5; disabled when empty) ---
    auth_secret: str = ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_secret)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the `.env` file is parsed once. Tests can clear the cache with
    ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()


# Convenience handle for call sites that don't need lazy loading.
settings = get_settings()
