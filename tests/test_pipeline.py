"""Tests for the voice loop, with STT/TTS faked (no network)."""

import app.pipeline as pipeline
from app import protocol


class FakeSink:
    """Collects everything the pipeline sends back over the socket."""

    def __init__(self):
        self.json: list[dict] = []
        self.bytes: list[bytes] = []

    async def send_json(self, payload: dict) -> None:
        self.json.append(payload)

    async def send_bytes(self, data: bytes) -> None:
        self.bytes.append(data)


async def test_run_turn_streams_sentence_audio(monkeypatch):
    async def fake_transcribe(audio, **kw):
        return "test input"

    async def fake_synthesize(text):
        return f"AUDIO[{text}]".encode()

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)

    sink = FakeSink()
    spoken = await pipeline.run_turn(b"raw-audio", sink.send_json, sink.send_bytes)

    # The canned reply is multi-sentence, so we should get >= 2 audio frames.
    assert spoken >= 2
    assert len(sink.bytes) == spoken

    types = [m["type"] for m in sink.json]
    assert types[0] == protocol.TRANSCRIPT
    assert protocol.THINKING in types
    assert protocol.AUDIO_CHUNK in types
    assert types[-1] == protocol.TURN_COMPLETE

    # Every audio_chunk metadata frame is immediately followed by a binary frame
    # (we can't assert ordering across the two sinks, but counts must match).
    chunk_frames = [m for m in sink.json if m["type"] == protocol.AUDIO_CHUNK]
    assert len(chunk_frames) == len(sink.bytes)
    # Indices are sequential starting at 0.
    assert [c["index"] for c in chunk_frames] == list(range(spoken))


async def test_run_turn_respects_stt_flag(monkeypatch):
    calls = {"stt": 0}

    async def fake_transcribe(audio, **kw):
        calls["stt"] += 1
        return "should not be called"

    async def fake_synthesize(text):
        return b"x"

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
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
