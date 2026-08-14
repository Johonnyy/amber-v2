"""End-to-end WebSocket tests through the real ASGI app (STT/TTS/LLM faked).

Covers the Phase-1 happy path plus Phase-5 reliability: the session handshake,
reconnect/resume, auth, and the cost guardrails.
"""

import pytest
from fastapi.testclient import TestClient

import app.config as config_module
import app.pipeline as pipeline
import app.session as session_module
from app import protocol
from app.main import _authorized, app


@pytest.fixture
def faked_io(monkeypatch):
    async def fake_transcribe(audio, **kw):
        return "hello amber"

    async def fake_synthesize(text):
        return f"AUDIO[{text}]".encode()

    async def fake_think(messages, system=None, **kwargs):
        # Multi-sentence so the streaming seam produces >= 2 audio frames.
        for chunk in ["Hello back. ", "Good to hear from you."]:
            yield chunk

    async def no_context(query=None, **kw):
        return None, []

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "remember", no_remember)


@pytest.fixture
def fresh_caches():
    """Reset the settings + session-manager singletons before and after a test so
    env-driven config takes effect and doesn't leak to other tests."""
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()
    yield
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()


def _read_ready(ws) -> dict:
    frame = ws.receive_json()
    assert frame["type"] == protocol.READY
    return frame


def _drain_turn(ws) -> dict:
    """Read frames until turn_complete, returning it (consuming audio pairs)."""
    for _ in range(50):
        frame = ws.receive_json()
        if frame["type"] == protocol.AUDIO_CHUNK:
            ws.receive_bytes()
            continue
        if frame["type"] == protocol.THINKING:
            continue
        if frame["type"] == protocol.TRANSCRIPT:
            continue
        if frame["type"] == protocol.TURN_COMPLETE:
            return frame
    raise AssertionError("never saw turn_complete")


# --- Phase 1 happy path ---

def test_full_turn_over_websocket(faked_io):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ready = _read_ready(ws)
        assert ready["session_id"]  # Phase 5: handshake carries a session id
        assert ready["resumed"] is False

        ws.send_bytes(b"pretend-this-is-audio")
        complete = _drain_turn(ws)
        assert complete["sentences"] >= 2


def test_awaiting_response_reaches_the_wire(fresh_caches, monkeypatch):
    """A turn whose brain signals expect_reply puts awaiting_response on the
    turn_complete frame a real client receives."""

    async def fake_transcribe(audio, **kw):
        return "play it"

    async def fake_synthesize(text):
        return f"AUDIO[{text}]".encode()

    async def signaling_think(messages, system=None, signals=None, **kwargs):
        signals.awaiting_response = True
        yield "Which one do you mean?"

    async def no_context(query=None, **kw):
        return None, []

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    monkeypatch.setattr(pipeline, "think", signaling_think)
    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "remember", no_remember)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        _read_ready(ws)
        ws.send_bytes(b"pretend-this-is-audio")
        complete = _drain_turn(ws)
        assert complete.get("awaiting_response") is True


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --- Phase 5: sessions & reconnect ---

def test_reconnect_resumes_session(faked_io, fresh_caches):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        first = _read_ready(ws)
        sid = first["session_id"]
        ws.send_bytes(b"audio")
        _drain_turn(ws)

    # Reconnect presenting the id -> same session resumed.
    with client.websocket_connect(f"/ws?session_id={sid}") as ws:
        again = _read_ready(ws)
        assert again["session_id"] == sid
        assert again["resumed"] is True


def test_unknown_session_id_starts_fresh(faked_io, fresh_caches):
    client = TestClient(app)
    with client.websocket_connect("/ws?session_id=never-issued") as ws:
        ready = _read_ready(ws)
        assert ready["session_id"] != "never-issued"
        assert ready["resumed"] is False


# --- Phase 5: auth ---

class _FakeWS:
    def __init__(self, query=None, headers=None):
        self.query_params = query or {}
        self.headers = headers or {}


def _settings(**over):
    return config_module.Settings(_env_file=None, **over)


def test_authorized_open_when_no_secret():
    assert _authorized(_FakeWS(), _settings(auth_secret="")) is True


def test_authorized_accepts_query_token():
    s = _settings(auth_secret="hunter2")
    assert _authorized(_FakeWS(query={"token": "hunter2"}), s) is True
    assert _authorized(_FakeWS(query={"token": "wrong"}), s) is False


def test_authorized_accepts_bearer_header():
    s = _settings(auth_secret="hunter2")
    ws = _FakeWS(headers={"authorization": "Bearer hunter2"})
    assert _authorized(ws, s) is True


def test_authorized_rejects_missing_token():
    assert _authorized(_FakeWS(), _settings(auth_secret="hunter2")) is False


def test_ws_connects_with_valid_token(faked_io, fresh_caches, monkeypatch):
    monkeypatch.setenv("AMBER_AUTH_SECRET", "letmein")
    config_module.get_settings.cache_clear()
    client = TestClient(app)
    with client.websocket_connect("/ws?token=letmein") as ws:
        assert _read_ready(ws)["session_id"]


# --- Phase 5: guardrails ---

def test_oversize_utterance_rejected(faked_io, fresh_caches, monkeypatch):
    monkeypatch.setenv("AMBER_MAX_AUDIO_BYTES", "8")
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        _read_ready(ws)
        ws.send_bytes(b"this is definitely more than eight bytes")
        frame = ws.receive_json()
        assert frame["type"] == protocol.ERROR
        assert frame["code"] == protocol.ERR_PAYLOAD_TOO_LARGE


def test_rate_limit_blocks_second_turn(faked_io, fresh_caches, monkeypatch):
    monkeypatch.setenv("AMBER_RATE_LIMIT_TURNS", "1")
    monkeypatch.setenv("AMBER_RATE_LIMIT_WINDOW_S", "60")
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        _read_ready(ws)
        ws.send_bytes(b"audio one")
        _drain_turn(ws)

        ws.send_bytes(b"audio two")  # over the per-window cap
        frame = ws.receive_json()
        assert frame["type"] == protocol.ERROR
        assert frame["code"] == protocol.ERR_RATE_LIMITED
