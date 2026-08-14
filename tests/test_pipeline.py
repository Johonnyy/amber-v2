"""Tests for the voice loop, with STT/TTS/LLM faked (no network)."""

import asyncio

import pytest

import app.pipeline as pipeline
from app import protocol
from app.session import Conversation


class FakeSink:
    """Collects everything the pipeline sends back over the socket."""

    def __init__(self):
        self.json: list[dict] = []
        self.bytes: list[bytes] = []

    async def send_json(self, payload: dict) -> None:
        self.json.append(payload)

    async def send_bytes(self, data: bytes) -> None:
        self.bytes.append(data)


@pytest.fixture
def fake_io(monkeypatch):
    """Fake STT + TTS (and neutralize memory) so no network/API key is needed."""

    async def fake_transcribe(audio, **kw):
        return "hello amber"

    async def fake_synthesize(text):
        return f"AUDIO[{text}]".encode()

    async def no_context(query=None, **kw):
        return None, []

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    # Memory defaults on; keep these pipeline tests focused (and offline) by
    # stubbing the read/write halves. Memory has its own dedicated tests.
    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "remember", no_remember)


def fake_brain(*chunks):
    """Build a fake `think` that streams the given text chunks."""

    async def think(messages, system=None, **kw):
        for chunk in chunks:
            yield chunk

    return think


async def test_run_turn_uses_llm_and_streams_audio(fake_io, monkeypatch):
    monkeypatch.setattr(
        pipeline, "think", fake_brain("Hi there. ", "How can I help?")
    )

    conv = Conversation()
    sink = FakeSink()
    spoken = await pipeline.run_turn(
        b"raw-audio", sink.send_json, sink.send_bytes, conv
    )

    # Two sentences -> two audio frames.
    assert spoken == 2
    assert len(sink.bytes) == spoken

    types = [m["type"] for m in sink.json]
    assert types[0] == protocol.TRANSCRIPT
    assert protocol.THINKING in types
    assert protocol.AUDIO_CHUNK in types
    assert types[-1] == protocol.TURN_COMPLETE

    chunk_frames = [m for m in sink.json if m["type"] == protocol.AUDIO_CHUNK]
    assert [c["index"] for c in chunk_frames] == list(range(spoken))

    # History records both sides of the exchange.
    assert conv.messages[0] == {"role": "user", "content": "hello amber"}
    assert conv.messages[1]["role"] == "assistant"
    assert "How can I help?" in conv.messages[1]["content"]


async def test_turn_complete_carries_awaiting_when_signaled(fake_io, monkeypatch):
    """When the brain flips the turn signal, turn_complete carries awaiting_response."""

    async def think(messages, system=None, signals=None, **kw):
        signals.awaiting_response = True
        yield "Which movie do you mean?"

    monkeypatch.setattr(pipeline, "think", think)

    conv = Conversation()
    sink = FakeSink()
    await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, conv)

    complete = next(m for m in sink.json if m["type"] == protocol.TURN_COMPLETE)
    assert complete.get("awaiting_response") is True


async def test_turn_complete_omits_awaiting_by_default(fake_io, monkeypatch):
    """An ordinary reply leaves the field off entirely (bare frame shape preserved)."""
    monkeypatch.setattr(pipeline, "think", fake_brain("Sure thing."))

    conv = Conversation()
    sink = FakeSink()
    await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, conv)

    complete = next(m for m in sink.json if m["type"] == protocol.TURN_COMPLETE)
    assert "awaiting_response" not in complete


async def test_canned_path_never_awaits(fake_io, monkeypatch):
    """The empty-transcript reprompt path never sets awaiting_response."""

    async def fake_transcribe(audio, **kw):
        return ""

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)

    conv = Conversation()
    sink = FakeSink()
    await pipeline.run_turn(b"silence", sink.send_json, sink.send_bytes, conv)

    complete = next(m for m in sink.json if m["type"] == protocol.TURN_COMPLETE)
    assert "awaiting_response" not in complete


async def test_history_persists_across_turns(fake_io, monkeypatch):
    seen_lengths: list[int] = []

    async def think(messages, system=None, **kw):
        # Record how much history the brain was handed each turn.
        seen_lengths.append(len(messages))
        yield "Okay."

    monkeypatch.setattr(pipeline, "think", think)

    conv = Conversation()
    sink = FakeSink()
    await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, conv)
    await pipeline.run_turn(b"b", sink.send_json, sink.send_bytes, conv)

    # Turn 1 saw just the new user turn; turn 2 saw user+assistant+user.
    assert seen_lengths == [1, 3]
    assert [m["role"] for m in conv.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


async def test_empty_transcript_reprompts_without_calling_llm(fake_io, monkeypatch):
    async def fake_transcribe(audio, **kw):
        return ""  # STT heard nothing

    async def boom(messages, system=None, **kw):
        raise AssertionError("LLM must not be called on an empty transcript")
        yield  # pragma: no cover

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "think", boom)

    conv = Conversation()
    sink = FakeSink()
    spoken = await pipeline.run_turn(b"silence", sink.send_json, sink.send_bytes, conv)

    assert spoken >= 1  # the reprompt was spoken
    assert conv.messages == []  # nothing polluted the history


async def test_interrupt_saves_partial_reply(fake_io, monkeypatch):
    streamed = asyncio.Event()

    async def think(messages, system=None, **kw):
        yield "First sentence. "
        yield "Second sentence "
        streamed.set()
        await asyncio.Event().wait()  # block forever, simulating a long reply

    monkeypatch.setattr(pipeline, "think", think)

    conv = Conversation()
    sink = FakeSink()
    task = asyncio.create_task(
        pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, conv)
    )

    await streamed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The partial reply Amber actually got through is preserved as context.
    assert conv.messages[0] == {"role": "user", "content": "hello amber"}
    assert conv.messages[-1]["role"] == "assistant"
    assert "First sentence." in conv.messages[-1]["content"]


async def test_run_turn_falls_back_to_canned_when_llm_disabled(
    fake_io, monkeypatch
):
    monkeypatch.setenv("AMBER_FEATURE_LLM", "false")
    pipeline.get_settings.cache_clear()

    async def boom(messages, system=None, **kw):
        raise AssertionError("LLM is disabled; think() must not run")
        yield  # pragma: no cover

    monkeypatch.setattr(pipeline, "think", boom)

    conv = Conversation()
    sink = FakeSink()
    try:
        spoken = await pipeline.run_turn(
            b"raw", sink.send_json, sink.send_bytes, conv
        )
        # The Phase-1 canned reply is multi-sentence.
        assert spoken >= 2
        assert conv.messages[0]["role"] == "user"
        assert conv.messages[-1]["role"] == "assistant"
    finally:
        pipeline.get_settings.cache_clear()


async def test_run_turn_respects_stt_flag(fake_io, monkeypatch):
    calls = {"stt": 0}

    async def fake_transcribe(audio, **kw):
        calls["stt"] += 1
        return "should not be called"

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setenv("AMBER_FEATURE_STT", "false")
    pipeline.get_settings.cache_clear()

    sink = FakeSink()
    try:
        await pipeline.run_turn(b"raw", sink.send_json, sink.send_bytes)
        assert calls["stt"] == 0  # STT skipped when flag is off
        transcript = next(m for m in sink.json if m["type"] == protocol.TRANSCRIPT)
        assert transcript["text"] == ""
    finally:
        pipeline.get_settings.cache_clear()
