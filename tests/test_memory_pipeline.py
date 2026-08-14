"""Integration: the pipeline reads memory into the prompt and writes it back.

STT/TTS and both memory halves are faked; this asserts the *wiring* — that the
context block reaches the brain's system prompt, and that the writer is called with
the real exchange only after a genuine turn.
"""

import pytest

import app.pipeline as pipeline
from app.session import Conversation


class FakeSink:
    def __init__(self):
        self.json = []
        self.bytes = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, data):
        self.bytes.append(data)


@pytest.fixture
def fake_io(monkeypatch):
    async def fake_transcribe(audio, **kw):
        return "I have a dog named Mango"

    async def fake_synthesize(text):
        return f"AUDIO[{text}]".encode()

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)


async def test_memory_block_reaches_the_brain_system_prompt(fake_io, monkeypatch):
    seen = {}

    async def fake_build_memory_view(query=None, **kw):
        seen["query"] = query
        block = "What you remember about your user:\n- Has a dog named Mango"
        return block, ["Has a dog named Mango"]

    async def fake_think(messages, system=None, **kw):
        seen["system"] = system
        yield "Hello there."

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "build_memory_view", fake_build_memory_view)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "remember", no_remember)

    sink = FakeSink()
    await pipeline.run_turn(b"audio", sink.send_json, sink.send_bytes, Conversation())

    # Context builder saw the user's transcript as its relevance query.
    assert seen["query"] == "I have a dog named Mango"
    # The memory block was appended to the persona prompt handed to the brain.
    assert "Has a dog named Mango" in seen["system"]
    assert "You are Amber" in seen["system"]  # persona still present
    # And the same facts were surfaced to the client as a memory frame.
    mem = [m for m in sink.json if m["type"] == "memory"]
    assert mem == [{"type": "memory", "items": ["Has a dog named Mango"]}]


async def test_runtime_context_reaches_the_brain_system_prompt(fake_io, monkeypatch):
    seen = {}

    async def no_view(query=None, **kw):
        return None, []

    async def fake_think(messages, system=None, **kw):
        seen["system"] = system
        yield "Sure."

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "build_memory_view", no_view)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "remember", no_remember)

    sink = FakeSink()
    await pipeline.run_turn(b"audio", sink.send_json, sink.send_bytes, Conversation())

    # The ambient date/time block is injected every turn, even with memory empty.
    assert "Right now it's" in seen["system"]
    assert "You are Amber" in seen["system"]  # persona still present


async def test_recap_requested_only_on_a_cold_session_start(fake_io, monkeypatch):
    recaps = []

    async def spy_view(query=None, *, include_recap=False, **kw):
        recaps.append(include_recap)
        return None, []

    async def fake_think(messages, system=None, **kw):
        yield "Okay."

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "build_memory_view", spy_view)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "remember", no_remember)

    conv = Conversation()
    sink = FakeSink()
    await pipeline.run_turn(b"a", sink.send_json, sink.send_bytes, conv)
    await pipeline.run_turn(b"b", sink.send_json, sink.send_bytes, conv)

    # First turn (empty history) asks for the recap; the second (warm) does not.
    assert recaps == [True, False]


async def test_writer_called_with_exchange_after_turn(fake_io, monkeypatch):
    calls = []

    async def no_context(query=None, **kw):
        return None, []

    async def fake_think(messages, system=None, **kw):
        yield "Nice, Mango sounds lovely."

    async def spy_remember(user_text, reply, **kw):
        calls.append((user_text, reply))
        return ["Has a dog named Mango"]

    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "remember", spy_remember)

    sink = FakeSink()
    await pipeline.run_turn(b"audio", sink.send_json, sink.send_bytes, Conversation())

    assert calls == [("I have a dog named Mango", "Nice, Mango sounds lovely.")]


async def test_writer_failure_does_not_break_the_turn(fake_io, monkeypatch):
    async def no_context(query=None, **kw):
        return None, []

    async def fake_think(messages, system=None, **kw):
        yield "All good."

    async def boom_remember(user_text, reply, **kw):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "remember", boom_remember)

    sink = FakeSink()
    # Must not raise — memory is best-effort.
    spoken = await pipeline.run_turn(
        b"audio", sink.send_json, sink.send_bytes, Conversation()
    )
    assert spoken >= 1
