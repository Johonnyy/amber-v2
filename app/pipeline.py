"""The voice loop — the entire contract, in one place.

    raw audio in  ->  STT  ->  brain (Claude stream / Phase-1 canned fallback)
        ->  sentence splitter  ->  TTS  ->  audio out, sentence by sentence

The streaming boundary (splitter -> TTS -> socket) is the performance-critical
seam: the first sentence is synthesized and sent while the rest of the response is
still being generated. An asyncio cancellation propagated from the WS handler (on
client ``interrupt`` or barge-in) stops synthesis mid-response.

Phase 2 adds the brain and conversation history. Phase 3 adds persistent memory
around it. Each turn:
  1. transcribe the utterance and record it as a user turn,
  2. pull relevant long-term memory into the system prompt (read half),
  3. stream the LLM's reply through the splitter/TTS seam,
  4. record what was actually spoken as an assistant turn — even on interrupt, so
     the history stays coherent ("you started to say X, then I cut you off"),
  5. *after* the audio and ``turn_complete`` are on the wire, distil and store new
     facts from the exchange (write half) — off the latency path.

History lives per-connection (see `app.session.Conversation`); it is the LLM's
short-term context, distinct from persistent memory (`app.memory`, SQLite), which
outlives the session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from app import protocol
from app.brain import think
from app.config import get_settings
from app.memory import build_memory_view, remember
from app.persona import compose_system_prompt
from app.responder import respond
from app.sentence_splitter import SentenceSplitter
from app.session import Conversation
from app.stt import transcribe
from app.tts import synthesize

if TYPE_CHECKING:
    from app.client_tools import ClientTools

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
    client_tools: "ClientTools | None" = None,
) -> int:
    """Process one user turn and stream the spoken reply back.

    ``conversation`` carries the per-connection history; if omitted a throwaway one
    is used (single-turn, no memory). ``client_tools`` is the connection's declared
    client-side tools (Phase 4+), offered to the brain alongside Amber's own.
    Returns the number of sentences spoken. Raises on transport/API failure so the
    caller can emit an error frame; ``asyncio.CancelledError`` from an interrupt is
    allowed to propagate untouched.
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
    reply = ""
    try:
        if not transcript_text:
            # Nothing heard (silence, or STT disabled) — reprompt without spending
            # an LLM call or feeding the brain an empty user turn.
            spoken = await _speak_stream(_canned(_DIDNT_CATCH), send_json, send_bytes)
        else:
            spoken, reply = await _think_and_speak(
                transcript_text, conversation, send_json, send_bytes, client_tools
            )
    finally:
        await send_json(protocol.thinking(False))

    await send_json(protocol.turn_complete(spoken))

    # 3. Write half of memory. Runs only after the audio + completion frame are
    # already sent, so a slow extraction can't delay speech. Best-effort: a failure
    # is logged, never surfaced. Skipped for the canned/empty paths (nothing real
    # to remember) and when memory or the LLM is off.
    if (
        settings.feature_memory
        and settings.feature_llm
        and transcript_text
        and reply
    ):
        await _remember_safe(transcript_text, reply)

    return spoken


async def _think_and_speak(
    transcript_text: str,
    conversation: Conversation,
    send_json: SendJson,
    send_bytes: SendBytes,
    client_tools: "ClientTools | None" = None,
) -> tuple[int, str]:
    """Record the user turn, stream a reply, and record what was spoken.

    Returns ``(sentences_spoken, reply_text)``; the caller uses the reply to feed
    the memory writer. The assistant turn is saved in a ``finally`` so an interrupt
    mid-response still persists the partial reply — the next turn's context reflects
    what the user actually heard.
    """
    settings = get_settings()
    conversation.add_user(transcript_text)

    # Phase 2: the brain. Fallback to the Phase-1 canned reply when the LLM is off
    # (no key / tests / demos) so the pipe still runs end to end.
    if settings.feature_llm:
        # Phase 3 read half: pull relevant long-term memory once — into the system
        # prompt (the model's copy) and out to the client as a ``memory`` frame (the
        # user-visible copy), so the two can't drift. The frame is advisory; emitting
        # it before the reply streams lets a client show what Amber is drawing on.
        memory_block, memory_items = await build_memory_view(transcript_text)
        if memory_items:
            await send_json(protocol.memory(memory_items))
        system = compose_system_prompt(memory_block)
        tokens = think(conversation.messages, system=system, client_tools=client_tools)
    else:
        tokens = respond(transcript_text)

    spoken_text: list[str] = []
    spoken = 0
    try:
        spoken = await _speak_stream(
            _capture(tokens, spoken_text), send_json, send_bytes
        )
    finally:
        reply = "".join(spoken_text).strip()
        if reply:
            conversation.add_assistant(reply)
    return spoken, reply


async def _remember_safe(user_text: str, reply: str) -> None:
    """Run the memory writer, swallowing failures so a turn never breaks on it.

    A genuine interrupt/barge-in (``CancelledError``) is re-raised so the turn
    unwinds normally; any other error is logged and dropped — memory is best-effort.
    """
    try:
        await remember(user_text, reply)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — memory must never take down a turn
        logger.exception("Memory write failed (non-fatal)")


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
