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

from agent_runtime import RunState

from app import protocol, signals
from app.brain import record_spend as brain_record_spend, think
from app.config import get_settings
from app.ecosystem import build_ecosystem_block
from app.memory import build_memory_view, build_notes_block, get_store, remember
from app.persona import compose_system_prompt
from app.responder import respond
from app.runtime_context import build_runtime_context
from app.sentence_splitter import SentenceSplitter
from app.session import Conversation
from app.stt import transcribe
from app.tts import synthesize
from app.turn_signals import TurnSignals
from app.voice import VoiceSettings

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
    audio: bytes | None,
    send_json: SendJson,
    send_bytes: SendBytes,
    conversation: Conversation | None = None,
    client_tools: "ClientTools | None" = None,
    *,
    conversation_id: str | None = None,
    text: str | None = None,
    voice: VoiceSettings | None = None,
    model: str | None = None,
) -> int:
    """Process one user turn and stream the spoken reply back.

    ``conversation`` carries the per-connection history; if omitted a throwaway one
    is used (single-turn, no memory). ``client_tools`` is the connection's declared
    client-side tools (Phase 4+), offered to the brain alongside Amber's own.
    ``conversation_id`` is the session id, passed down so this turn's model spend
    and tool calls are attributable to it in the usage tables, and so any peer agent
    Amber calls joins the same exchange.

    ``text`` is a typed turn (a ``user_text`` frame): the client already has the
    words, so they're taken verbatim and ``audio`` is ignored — the only difference
    is that STT is skipped. Everything from the ``transcript`` frame onward is the
    same path a spoken turn takes.

    ``voice`` is this connection's TTS settings (`app.voice`); omitted, the install
    default is used. It is read once here and threaded down, so a ``set_voice``
    landing mid-reply takes effect on the next turn rather than switching voices
    between two sentences of the same one.

    ``model`` is this connection's chosen model keyword (`app.models`), pinned for the
    turn for the same reason and omitted to use the install's own.

    Returns the number of sentences spoken. Raises on transport/API failure so the
    caller can emit an error frame; ``asyncio.CancelledError`` from an interrupt is
    allowed to propagate untouched.
    """
    settings = get_settings()
    conversation = conversation if conversation is not None else Conversation()
    # Pinned for the whole turn — see the docstring.
    voice = voice if voice is not None else VoiceSettings.from_settings(settings)

    # 1. Transcribe — or take the client's typed text verbatim, skipping STT.
    if text is not None:
        transcript_text = text
    elif settings.feature_stt:
        transcript_text = await transcribe(audio)
    else:
        transcript_text = ""
        logger.info("STT disabled (AMBER_FEATURE_STT=false); using canned greeting")
    # Echoed for typed turns too, so a client renders both input modes identically.
    await send_json(protocol.transcript(transcript_text))

    # 2. Think -> stream -> speak.
    await send_json(protocol.thinking(True))
    reply = ""
    # Turn-based conversations: did Amber ask something it expects an answer to?
    # The canned/empty path never awaits; the brain path sets this via expect_reply.
    awaiting = False
    used_fact_ids: list[int] = []
    stats: dict = {}
    try:
        if not transcript_text:
            # Nothing heard (silence, or STT disabled) — reprompt without spending
            # an LLM call or feeding the brain an empty user turn.
            spoken = await _speak_stream(
                _canned(_DIDNT_CATCH), send_json, send_bytes, voice
            )
        else:
            spoken, reply, awaiting, used_fact_ids, stats = await _think_and_speak(
                transcript_text,
                conversation,
                send_json,
                send_bytes,
                client_tools,
                conversation_id=conversation_id,
                voice=voice,
                model=model,
                # A typed turn reads on a screen; a spoken one is heard. Same words,
                # different register — see `app.persona`.
                modality="text" if text is not None else "voice",
            )
    finally:
        await send_json(protocol.thinking(False))

    await send_json(
        protocol.turn_complete(spoken, awaiting_response=awaiting, **stats)
    )

    # Telemetry, queued rather than written — nothing here waits on SQLite. A
    # correction is the strongest signal Amber got something wrong, and the only one
    # that arrives without the user ever saying so directly.
    signals.record(
        signals.KIND_TURN,
        name="text" if text is not None else "voice",
        ok=bool(reply),
        latency_ms=None,
        detail=f"sentences={spoken} facts={len(used_fact_ids)}",
        session_id=conversation_id,
    )
    if transcript_text and signals.looks_like_a_correction(transcript_text):
        signals.record(
            signals.KIND_CORRECTION,
            detail=transcript_text,
            session_id=conversation_id,
        )

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
        # Record which facts earned their place first — a cheap local write, before
        # the fallible model call, so a barge-in mid-extraction loses only the
        # extraction. This is the sole evidence memory has that a fact was useful,
        # and it's what drives promotion and decay.
        await _touch_safe(used_fact_ids)
        await _remember_safe(transcript_text, reply, conversation_id=conversation_id)

    return spoken


async def _think_and_speak(
    transcript_text: str,
    conversation: Conversation,
    send_json: SendJson,
    send_bytes: SendBytes,
    client_tools: "ClientTools | None" = None,
    *,
    conversation_id: str | None = None,
    modality: str = "voice",
    voice: VoiceSettings | None = None,
    model: str | None = None,
) -> tuple[int, str, bool, list[int]]:
    """Record the user turn, stream a reply, and record what was spoken.

    Returns ``(sentences_spoken, reply_text, awaiting_response, used_fact_ids)``; the
    caller uses the reply to feed the memory writer, the flag to set
    ``turn_complete``, and the ids to mark those facts as used once the turn lands.
    The assistant turn is saved in a ``finally`` so an interrupt mid-response still
    persists the partial reply — the next turn's context reflects what the user
    actually heard. ``awaiting_response`` is the brain's turn-based signal (the
    model called ``expect_reply``); the canned ``respond`` fallback never sets it.
    """
    settings = get_settings()
    signals = TurnSignals()
    used_fact_ids: list[int] = []
    # Collects what each model call actually cost. Passed to the runner and read
    # after the stream drains — see `brain.record_spend`.
    state = RunState()
    # A cold turn — the first of a fresh or reconnected session, before this user
    # turn is recorded — has no live history yet, so the brain gets a "where you left
    # off" recap from durable memory. Once the session has turns of its own they
    # already carry recent context, so the recap is skipped to avoid replaying them.
    cold_start = not conversation.messages
    conversation.add_user(transcript_text)

    # Phase 2: the brain. Fallback to the Phase-1 canned reply when the LLM is off
    # (no key / tests / demos) so the pipe still runs end to end.
    if settings.feature_llm:
        # Phase 3 read half: pull relevant long-term memory once — into the system
        # prompt (the model's copy) and out to the client as a ``memory`` frame (the
        # user-visible copy), so the two can't drift. The frame is advisory; emitting
        # it before the reply streams lets a client show what Amber is drawing on.
        view = await build_memory_view(transcript_text, include_recap=cold_start)
        used_fact_ids = view.fact_ids
        if view.items:
            await send_json(protocol.memory(view.items, view.facts))
        # Ambient context (date/time) is rebuilt every turn and always injected,
        # independent of the memory flag — Amber should always know when it is.
        runtime_context = build_runtime_context()
        system = compose_system_prompt(
            runtime_context=runtime_context,
            memory_block=view.block,
            modality=modality,
            client_tool_names=client_tools.names() if client_tools else (),
            # What the maintenance pass has noticed about how conversations go.
            # None unless AMBER_FEATURE_SELF_NOTES is on.
            notes_block=await build_notes_block(),
            # What she and the software around her are, plus what this install can
            # actually reach. Rebuilt per turn (it's only string joins) so a config
            # change lands without a restart. None unless the flag is on.
            ecosystem_block=build_ecosystem_block(),
        )
        tokens = think(
            conversation.messages,
            system=system,
            client_tools=client_tools,
            signals=signals,
            conversation_id=conversation_id,
            model=model,
            # The same sink the reply travels on. Tool frames are emitted from inside
            # the broker, which runs on this very task — so they interleave with
            # `audio_chunk` in true order without a queue or a lock.
            activity=send_json if settings.feature_activity_stream else None,
            # Collects each step's model, tokens, cost and timings — recorded below,
            # after the reply is out. See `brain.record_spend`.
            state=state,
        )
    else:
        tokens = respond(transcript_text)

    spoken_text: list[str] = []
    spoken = 0
    try:
        spoken = await _speak_stream(
            _capture(
                tokens,
                spoken_text,
                send_json if settings.feature_text_stream else None,
            ),
            send_json,
            send_bytes,
            voice,
        )
    finally:
        reply = "".join(spoken_text).strip()
        if reply:
            conversation.add_assistant(reply)
        # Here rather than inside the stream, and in the same `finally` that persists
        # a partial reply, for the same reason: an interrupted turn still happened and
        # still cost money. `record_spend` never raises.
        cost = await brain_record_spend(state, conversation_id=conversation_id)
    return spoken, reply, signals.awaiting_response, used_fact_ids, _stats(state, cost)


def _stats(state: RunState, cost: float) -> dict:
    """Per-turn numbers for `turn_complete`, or nothing at all.

    Empty when there were no steps, so the frame keeps the exact shape it had before
    on the canned path and on any install where cost tracking is off. That is the
    difference between an additive field and a changed one.
    """
    if not state.steps:
        return {}
    return {
        "steps": len(state.steps),
        "tokens_in": sum(step.tokens_in for step in state.steps),
        "tokens_out": sum(step.tokens_out for step in state.steps),
        "cost_usd": cost,
        # The model that actually answered, which is not always what the keyword
        # resolved to at the start — the table can be re-pointed between turns.
        "model": state.steps[-1].model,
    }


async def _remember_safe(
    user_text: str, reply: str, *, conversation_id: str | None = None
) -> None:
    """Run the memory writer, swallowing failures so a turn never breaks on it.

    A genuine interrupt/barge-in (``CancelledError``) is re-raised so the turn
    unwinds normally; any other error is logged and dropped — memory is best-effort.
    """
    try:
        await remember(user_text, reply, conversation_id=conversation_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — memory must never take down a turn
        logger.exception("Memory write failed (non-fatal)")


async def _touch_safe(fact_ids: list[int]) -> None:
    """Mark the facts this turn drew on as used. Never fatal, like every memory write."""
    if not fact_ids:
        return
    try:
        await asyncio.to_thread(get_store().touch_facts, fact_ids)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — memory must never take down a turn
        logger.exception("Memory touch failed (non-fatal)")


async def _capture(
    tokens: AsyncIterator[str],
    sink: list[str],
    send_json: SendJson | None = None,
) -> AsyncIterator[str]:
    """Pass tokens through unchanged while accumulating them for history.

    Also the tee point for the ``delta`` frame, when ``send_json`` is given. Here
    rather than anywhere downstream because this is the last place the model's own
    text still exists as the model wrote it: the very next stage is the sentence
    splitter, which is where newlines, headings and code fences stop being
    recoverable. ``audio_chunk`` carries sentences because *speech* is made of
    sentences; a screen is not, and reconstructing one from the other is guesswork.

    Deliberately not wrapped in a try: a send that fails here fails for the same
    reason the next ``audio_chunk`` would, and swallowing it would only delay the
    turn's error by one sentence.
    """
    async for chunk in tokens:
        sink.append(chunk)
        if send_json is not None and chunk:
            await send_json(protocol.delta(chunk))
        yield chunk


async def _canned(text: str) -> AsyncIterator[str]:
    """Yield a fixed reply as a one-shot 'stream' (for non-LLM paths)."""
    yield text


async def _speak_stream(
    tokens: AsyncIterator[str],
    send_json: SendJson,
    send_bytes: SendBytes,
    voice: VoiceSettings | None = None,
) -> int:
    """Run a token stream through the splitter, synthesizing & sending each sentence."""
    voice = voice if voice is not None else VoiceSettings.from_settings()
    splitter = SentenceSplitter()
    index = 0

    async def emit(sentence: str) -> None:
        nonlocal index
        audio_bytes = await synthesize(sentence, voice)
        # The container comes from the voice actually used, not from config — a
        # client that asked for wav must not be told the bytes are mp3.
        await send_json(protocol.audio_chunk(index, sentence, voice.audio_format))
        await send_bytes(audio_bytes)
        index += 1

    async for chunk in tokens:
        for sentence in splitter.feed(chunk):
            await emit(sentence)
    for sentence in splitter.flush():
        await emit(sentence)

    return index
