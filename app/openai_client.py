"""Shared AsyncOpenAI client.

STT and TTS both talk to OpenAI; they share one client so the connection pool and
API key are configured in exactly one place (routed through `settings`).
"""

from __future__ import annotations

from functools import lru_cache

from openai import AsyncOpenAI

from app.config import get_settings


@lru_cache
def get_client() -> AsyncOpenAI:
    settings = get_settings()
    return AsyncOpenAI(api_key=settings.openai_api_key)
