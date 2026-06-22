"""The brain — Claude (Haiku) as a streamed token source.

This is the Phase-2 replacement for `app.responder`. It takes the per-connection
conversation history and streams Amber's reply back token by token, so the
pipeline's sentence splitter can start TTS on the first sentence before the whole
response exists. The contract is identical to `responder.respond`: an
``AsyncIterator[str]`` of text chunks. Everything downstream is unchanged.

Model, key, and token cap are all config-driven (`settings.llm_model`,
`settings.anthropic_api_key`, `settings.llm_max_tokens`) so the brain is swappable
without touching call sites.

No extended thinking is used: a voice loop is latency-sensitive and wants the
first spoken sentence out fast, so we stream a direct reply.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from functools import lru_cache

from anthropic import AsyncAnthropic

from app.config import get_settings
from app.persona import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


@lru_cache
def get_client() -> AsyncAnthropic:
    """Process-wide Anthropic client (connection pool + key configured once)."""
    settings = get_settings()
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def think(messages: list[dict]) -> AsyncIterator[str]:
    """Stream Amber's reply for the given conversation history.

    ``messages`` is the Anthropic message list (alternating/​combinable
    user/assistant turns); the persona system prompt is injected here, not stored
    in the history. Yields text deltas as they arrive.
    """
    settings = get_settings()
    client = get_client()

    logger.debug("LLM: %d message(s) -> %s", len(messages), settings.llm_model)
    async with client.messages.stream(
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        system=SYSTEM_PROMPT,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text
