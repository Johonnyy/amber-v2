"""Phase 1 stand-in for the brain.

There is no LLM yet (that's Phase 2). To prove the pipe end to end — and to exercise
the streaming sentence splitter the way a real token stream will — this yields a
short, multi-sentence reply as a sequence of small text chunks, mimicking an LLM
token stream. Phase 2 replaces this generator with the actual Claude stream; the
pipeline downstream of it does not change.
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
            f"Once my brain comes online in Phase 2, I'll actually respond."
        )
    return (
        "Hello! I'm Amber, and the voice pipeline is working. "
        "I didn't quite catch any words that time. "
        "Try speaking again once my brain comes online in Phase 2."
    )


async def respond(transcript: str) -> AsyncIterator[str]:
    """Yield the reply as word-sized chunks, mimicking a streamed token feed."""
    text = _greeting(transcript)
    for word in text.split(" "):
        yield word + " "
        # Tiny delay so the streaming seam is observable and cancellation has a
        # place to take effect; negligible for real latency.
        await asyncio.sleep(0.005)
