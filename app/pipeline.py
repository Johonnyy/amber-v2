"""The voice loop — the entire contract, in one place.

    raw audio in  ->  STT  ->  brain (Claude stream / Phase-1 canned fallback)
        ->  sentence splitter  ->  TTS  ->  audio out, sentence by sentence

The streaming boundary (splitter -> TTS -> socket) is the performance-critical
seam: the first sentence is synthesized and sent while the rest of the response is
still being generated. An asyncio cancellation propagated from the WS handler (on
client ``interrupt`` or barge-in) stops synthesis mid-response.

Phase 2 adds the brain and conversation history. Each turn:
  1. transcribe the utterance and record it as a user turn,
  2. stream the LLM's reply through the splitter/TTS seam,
  3. record what was actually spoken as an assistant turn — even on interrupt, so
     the history stays coherent ("you started to say X, then I cut you off").

History lives per-connection (see `app.session.Conversation`); it is the LLM's
short-term context, distinct from persistent memory (Phase 3).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from app import protocol
from app.brain import think
from app.config import get_settings
from app.responder import respond
from app.sentence_splitter import SentenceSplitter
from app.session import Conversation
from app.stt import transcribe
from app.tts import synthesize

logger = logging.getLogger(__name__)

# Sinks the pipeline writes to. The WS handler supplies real implementations;
# tests supply fakes. Keeping I/O behind these callables makes the loop testable.
SendJson = Callable[[dict], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]

# Spoken when STT returns nothing intelligible — we skip the LLM round trip (and
# don't pollute history) rather than send it an empty user turn.
_DIDNT_CATCH = "Sorry, I didn't catch that. Could you say it again?"


async def run_turn(
    audio: bytes,
    send_json: SendJson,
    send_bytes: SendBytes,
    conversation: Conversation | None = None,
) -> int:
    """Process one user turn and stream the spoken reply back.

    ``conversation`` carries the per-connection history; if omitted a throwaway one
    is used (single-turn, no memory). Returns the number of sentences spoken.
    Raises on transport/API failure so the caller can emit an error frame;
    ``asyncio.CancelledError`` from an interrupt is allowed to propagate untouched.
    """
    settings = get_settings()
    conversation = conversation if conversation is not None else Conversation()

    # 1. Transcribe (or skip, per feature flag).
    if settings.feature_stt:
        transcript_text = await transcribe(audio)
    else:
        transcript_text = ""
        logger.info("STT disabled (AMBER_FEATURE_STT=false); using canned greeting")
    await send_json(protocol.transcript(transcript_text))

    # 2. Think -> stream -> speak.
    await send_json(protocol.thinking(True))
    try:
        if not transcript_text:
            # Nothing heard (silence, or STT disabled) — reprompt without spending
            # an LLM call or feeding the brain an empty user turn.
            spoken = await _speak_stream(_canned(_DIDNT_CATCH), send_json, send_bytes)
        else:
            spoken = await _think_and_speak(
                transcript_text, conversation, send_json, send_bytes
            )
    finally:
        await send_json(protocol.thinking(False))

    await send_json(protocol.turn_complete(spoken))
    return spoken


async def _think_and_speak(
    transcript_text: str,
    conversation: Conversation,
    send_json: SendJson,
    send_bytes: SendBytes,
) -> int:
    """Record the user turn, stream a reply, and record what was spoken.

    The assistant turn is saved in a ``finally`` so an interrupt mid-response still
    persists the partial reply — the next turn's context reflects what the user
    actually heard.
    """
    settings = get_settings()
    conversation.add_user(transcript_text)

    # Phase 2: the brain. Fallback to the Phase-1 canned reply when the LLM is off
    # (no key / tests / demos) so the pipe still runs end to end.
    if settings.feature_llm:
        tokens = think(conversation.messages)
    else:
        tokens = respond(transcript_text)

    spoken_text: list[str] = []
    try:
        return await _speak_stream(
            _capture(tokens, spoken_text), send_json, send_bytes
        )
    finally:
        reply = "".join(spoken_text).strip()
        if reply:
            conversation.add_assistant(reply)


async def _capture(
    tokens: AsyncIterator[str], sink: list[str]
) -> AsyncIterator[str]:
    """Pass tokens through unchanged while accumulating them for history."""
    async for chunk in tokens:
        sink.append(chunk)
        yield chunk


async def _canned(text: str) -> AsyncIterator[str]:
    """Yield a fixed reply as a one-shot 'stream' (for non-LLM paths)."""
    yield text


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
