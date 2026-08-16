"""Tests for the voice loop, with STT/TTS/LLM faked (no network)."""

import asyncio

import pytest

import app.pipeline as pipeline
from app import protocol
from app.session import Conversation
from app.memory import MemoryView


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

    async def fake_synthesize(text, voice=None):
        return f"AUDIO[{text}]".encode()

    async def no_context(query=None, **kw):
        return MemoryView()

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


# --- typed turns (the ``user_text`` frame) ----------------------------------


async def test_typed_text_skips_stt_and_echoes_transcript(fake_io, monkeypatch):
    """A typed turn takes the client's words verbatim and never calls STT."""
    calls = {"stt": 0}

    async def counting_transcribe(audio, **kw):
        calls["stt"] += 1
        return "should not be called"

    monkeypatch.setattr(pipeline, "transcribe", counting_transcribe)
    monkeypatch.setattr(pipeline, "think", fake_brain("Half past four."))

    conv = Conversation()
    sink = FakeSink()
    spoken = await pipeline.run_turn(
        None, sink.send_json, sink.send_bytes, conv, text="what time is it"
    )

    assert calls["stt"] == 0
    transcript = next(m for m in sink.json if m["type"] == protocol.TRANSCRIPT)
    assert transcript["text"] == "what time is it"

    # From here on it is an ordinary turn: spoken reply, history, completion frame.
    assert spoken == 1
    assert len(sink.bytes) == spoken
    assert sink.json[-1]["type"] == protocol.TURN_COMPLETE
    assert conv.messages[0] == {"role": "user", "content": "what time is it"}
    assert conv.messages[1]["role"] == "assistant"


async def test_typed_text_wins_over_audio(fake_io, monkeypatch):
    """``text`` is authoritative — supplied audio is ignored, not transcribed."""
    monkeypatch.setattr(pipeline, "think", fake_brain("Sure."))

    sink = FakeSink()
    await pipeline.run_turn(
        b"raw-audio", sink.send_json, sink.send_bytes, Conversation(), text="typed"
    )

    transcript = next(m for m in sink.json if m["type"] == protocol.TRANSCRIPT)
    assert transcript["text"] == "typed"  # not the fake STT's "hello amber"


async def test_typed_text_ignores_stt_flag(fake_io, monkeypatch):
    """The STT flag governs transcription, not typed input — text still gets through."""
    monkeypatch.setattr(pipeline, "think", fake_brain("Understood."))
    monkeypatch.setenv("AMBER_FEATURE_STT", "false")
    pipeline.get_settings.cache_clear()

    conv = Conversation()
    sink = FakeSink()
    try:
        await pipeline.run_turn(
            None, sink.send_json, sink.send_bytes, conv, text="still works"
        )
        transcript = next(m for m in sink.json if m["type"] == protocol.TRANSCRIPT)
        # Without the typed path this would be "" and hit the reprompt instead.
        assert transcript["text"] == "still works"
        assert conv.messages[0]["content"] == "still works"
    finally:
        pipeline.get_settings.cache_clear()


# --- delta: the text view of a reply ---------------------------------------- #


async def test_deltas_carry_the_models_own_whitespace(fake_io, monkeypatch):
    """The whole reason the frame exists.

    ``audio_chunk`` carries sentences because speech is made of sentences; a client
    that concatenates them has to guess the whitespace between, so a heading or a
    list is gone by the time it reaches a screen. The delta stream is the same reply
    with the newlines still in it.
    """
    monkeypatch.setattr(
        pipeline, "think", fake_brain("## Results\n\n", "- one\n", "- two\n")
    )

    sink = FakeSink()
    await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, Conversation())

    raw = "".join(m["text"] for m in sink.json if m["type"] == protocol.DELTA)
    assert raw == "## Results\n\n- one\n- two\n"

    # The sentence path, meanwhile, has flattened it — which is correct for speech.
    spoken = " ".join(
        m["text"] for m in sink.json if m["type"] == protocol.AUDIO_CHUNK
    )
    assert "\n" not in spoken


async def test_deltas_precede_the_audio_they_describe(fake_io, monkeypatch):
    """Order matters: a client renders text, then hears it.

    Both frames go out on one task with no queue between them, so this is a real
    guarantee rather than a race that usually lands the right way up.
    """
    monkeypatch.setattr(pipeline, "think", fake_brain("First. ", "Second. "))

    sink = FakeSink()
    await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, Conversation())

    types = [m["type"] for m in sink.json]
    assert types.index(protocol.DELTA) < types.index(protocol.AUDIO_CHUNK)


async def test_the_text_stream_can_be_turned_off(fake_io, monkeypatch):
    """Off means the frames are simply never sent — every client behaves as before."""
    monkeypatch.setattr(pipeline, "think", fake_brain("Sure."))
    monkeypatch.setenv("AMBER_FEATURE_TEXT_STREAM", "false")
    pipeline.get_settings.cache_clear()

    sink = FakeSink()
    try:
        await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, Conversation())
        assert not [m for m in sink.json if m["type"] == protocol.DELTA]
        # ...and the reply still arrives by the path that predates it.
        assert [m for m in sink.json if m["type"] == protocol.AUDIO_CHUNK]
    finally:
        pipeline.get_settings.cache_clear()


async def test_the_activity_sink_reaches_the_brain(fake_io, monkeypatch):
    """`run_turn` hands the brain the same socket the reply travels on."""
    seen = {}

    async def think(messages, system=None, activity=None, **kw):
        seen["activity"] = activity
        yield "Done."

    monkeypatch.setattr(pipeline, "think", think)

    sink = FakeSink()
    await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, Conversation())

    assert seen["activity"] is not None
    # Prove it is a live sink rather than merely non-None.
    await seen["activity"](protocol.activity("c1", "start", "web_search", origin="own"))
    assert sink.json[-1]["name"] == "web_search"


async def test_the_activity_stream_can_be_turned_off(fake_io, monkeypatch):
    seen = {}

    async def think(messages, system=None, activity=None, **kw):
        seen["activity"] = activity
        yield "Done."

    monkeypatch.setattr(pipeline, "think", think)
    monkeypatch.setenv("AMBER_FEATURE_ACTIVITY_STREAM", "false")
    pipeline.get_settings.cache_clear()

    sink = FakeSink()
    try:
        await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, Conversation())
        assert seen["activity"] is None
    finally:
        pipeline.get_settings.cache_clear()
