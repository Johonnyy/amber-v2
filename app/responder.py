"""Canned fallback brain — used when the LLM is disabled.

Phase 2 added the real brain (`app.brain`, Claude). This module remains as the
fallback the pipeline uses when ``AMBER_FEATURE_LLM=false`` (no Anthropic key, or
tests/demos): it yields a short, multi-sentence reply as a sequence of small text
chunks, mimicking a token stream, so the pipe still runs end to end and exercises
the streaming sentence splitter. Same ``AsyncIterator[str]`` contract as
`app.brain.think`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


def _greeting(transcript: str) -> str:
    heard = transcript.strip()
    if heard:
        return (
            f"Hello! I'm Amber, and the voice pipeline is working. "
            f"I heard you say: {heard}. "
            f"My brain is switched off right now, so this is a canned reply."
        )
    return (
        "Hello! I'm Amber, and the voice pipeline is working. "
        "I didn't quite catch any words that time. "
        "Try speaking again."
    )


async def respond(transcript: str) -> AsyncIterator[str]:
    """Yield the reply as word-sized chunks, mimicking a streamed token feed."""
    text = _greeting(transcript)
    for word in text.split(" "):
        yield word + " "
        # Tiny delay so the streaming seam is observable and cancellation has a
        # place to take effect; negligible for real latency.
        await asyncio.sleep(0.005)
