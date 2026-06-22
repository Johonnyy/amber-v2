"""End-to-end WebSocket smoke test through the real ASGI app (STT/TTS faked)."""

import pytest
from fastapi.testclient import TestClient

import app.pipeline as pipeline
from app import protocol
from app.main import app


@pytest.fixture
def faked_io(monkeypatch):
    async def fake_transcribe(audio, **kw):
        return "hello amber"

    async def fake_synthesize(text):
        return f"AUDIO[{text}]".encode()

    async def fake_think(messages, system=None):
        # Multi-sentence so the streaming seam produces >= 2 audio frames.
        for chunk in ["Hello back. ", "Good to hear from you."]:
            yield chunk

    async def no_context(query=None, **kw):
        return None

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    monkeypatch.setattr(pipeline, "think", fake_think)
    # Keep the WS smoke test offline: stub the memory read/write halves.
    monkeypatch.setattr(pipeline, "build_context", no_context)
    monkeypatch.setattr(pipeline, "remember", no_remember)


def test_full_turn_over_websocket(faked_io):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == protocol.ready()

        ws.send_bytes(b"pretend-this-is-audio")

        # transcript first
        msg = ws.receive_json()
        assert msg["type"] == protocol.TRANSCRIPT
        assert msg["text"] == "hello amber"

        # thinking(true)
        assert ws.receive_json() == protocol.thinking(True)

        # at least one (audio_chunk metadata frame -> binary frame) pair
        first_chunk = ws.receive_json()
        assert first_chunk["type"] == protocol.AUDIO_CHUNK
        assert first_chunk["index"] == 0
        assert first_chunk["format"] == "mp3"
        audio = ws.receive_bytes()
        assert audio.startswith(b"AUDIO[")

        # drain until turn_complete
        saw_complete = False
        for _ in range(50):
            frame = ws.receive_json()
            if frame["type"] == protocol.AUDIO_CHUNK:
                ws.receive_bytes()
                continue
            if frame["type"] == protocol.THINKING:
                continue
            if frame["type"] == protocol.TURN_COMPLETE:
                assert frame["sentences"] >= 2
                saw_complete = True
                break
        assert saw_complete


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
