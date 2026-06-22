"""The voice loop — the entire Phase 1 contract, in one place.

    raw audio in  ->  STT  ->  responder (Phase 1 canned / Phase 2 LLM)
        ->  sentence splitter  ->  TTS  ->  audio out, sentence by sentence

The streaming boundary (splitter -> TTS -> socket) is the performance-critical
seam: the first sentence is synthesized and sent while the rest of the response is
still being generated. An asyncio cancellation propagated from the WS handler (on
client ``interrupt``) stops synthesis mid-response.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from app import protocol
from app.config import get_settings
from app.responder import respond
from app.sentence_splitter import SentenceSplitter
from app.stt import transcribe
from app.tts import synthesize

logger = logging.getLogger(__name__)

# Sinks the pipeline writes to. The WS handler supplies real implementations;
# tests supply fakes. Keeping I/O behind these callables makes the loop testable.
SendJson = Callable[[dict], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


async def run_turn(
    audio: bytes,
    send_json: SendJson,
    send_bytes: SendBytes,
) -> int:
    """Process one user turn and stream the spoken reply back.

    Returns the number of sentences spoken. Raises on transport/API failure so the
    caller can emit an error frame; ``asyncio.CancelledError`` from an interrupt is
    allowed to propagate untouched.
    """
    settings = get_settings()

    # 1. Transcribe (or skip, per feature flag).
    if settings.feature_stt:
        transcript_text = await transcribe(audio)
    else:
        transcript_text = ""
        logger.info("STT disabled (AMBER_FEATURE_STT=false); using canned greeting")
    await send_json(protocol.transcript(transcript_text))

    # 2. Think. Phase 1: canned. Phase 2: swap `respond` for the LLM stream.
    await send_json(protocol.thinking(True))
    token_stream = respond(transcript_text)

    # 3. Stream tokens -> sentences -> TTS -> client.
    spoken = await _speak_stream(token_stream, send_json, send_bytes)

    await send_json(protocol.thinking(False))
    await send_json(protocol.turn_complete(spoken))
    return spoken


async def _speak_stream(
    tokens: AsyncIterator[str],
    send_json: SendJson,
    send_bytes: SendBytes,
) -> int:
    """Run a token stream through the splitter, synthesizing & sending each sentence."""
    settings = get_settings()
    splitter = SentenceSplitter()
    index = 0

    async def emit(sentence: str) -> None:
        nonlocal index
        audio_bytes = await synthesize(sentence)
        await send_json(protocol.audio_chunk(index, sentence, settings.tts_format))
        await send_bytes(audio_bytes)
        index += 1

    async for chunk in tokens:
        for sentence in splitter.feed(chunk):
            await emit(sentence)
    for sentence in splitter.flush():
        await emit(sentence)

    return index
