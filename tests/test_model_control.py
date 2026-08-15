"""Choosing the brain over the wire — the ``set_model`` / ``model`` frame pair.

The peer of `test_voice.py`, and it checks the same two things that pair exists for:
that a client's request actually reaches the model call, and that the frame coming
back is the truth about what took effect rather than an echo of what was asked.

The distinction this file leans on hardest is *scope*. ``keyword`` is per-connection
and lives on the session; ``map`` is per-install and lives in SQLite. They arrive in
one frame and must not be conflated — one follows the socket, the other outlives it.
"""

import pytest
from fastapi.testclient import TestClient

import app.config as config_module
import app.pipeline as pipeline
import app.session as session_module
from app import models, protocol
from app.main import app
from app.memory import MemoryView


@pytest.fixture
def faked_io(monkeypatch):
    """The pipeline with STT/TTS/LLM faked, capturing the keyword each turn used."""
    used: list[str | None] = []

    async def fake_transcribe(audio, **kw):
        return "hello amber"

    async def fake_synthesize(text, voice=None):
        return b"AUDIO"

    async def fake_think(messages, system=None, **kwargs):
        used.append(kwargs.get("model"))
        yield "Sure thing."

    async def no_context(query=None, **kw):
        return MemoryView()

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "remember", no_remember)
    return used


@pytest.fixture(autouse=True)
def clean_keywords():
    """Undo any re-pointing a test does.

    These write to the *process-wide* store (that is the feature — an override has to
    outlive the connection), so without this one test's ``coding`` would be another's
    starting state.
    """
    yield
    from app.memory import get_store

    for keyword in list(get_store().model_keywords()):
        get_store().clear_model_keyword(keyword)
    models.invalidate_cache()


@pytest.fixture
def fresh_caches():
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()
    yield
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()


def _handshake(ws) -> dict:
    """Consume ``ready`` + ``voice`` + ``model``, returning the model frame."""
    assert ws.receive_json()["type"] == protocol.READY
    assert ws.receive_json()["type"] == protocol.VOICE
    frame = ws.receive_json()
    assert frame["type"] == protocol.MODEL
    return frame


def _speak(ws) -> None:
    """Run one turn to completion, discarding its frames."""
    ws.send_bytes(b"audio")
    for _ in range(30):
        frame = ws.receive_json()
        if frame["type"] == protocol.AUDIO_CHUNK:
            ws.receive_bytes()
        elif frame["type"] == protocol.TURN_COMPLETE:
            return
    raise AssertionError("turn never completed")


# --- the handshake ---


def test_model_frame_follows_voice_with_the_catalogue(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _handshake(ws)
        assert frame["settings"]["keyword"] == "balanced"
        assert frame["settings"]["chosen"] is False
        names = [k["name"] for k in frame["options"]["keywords"]]
        assert "coding" in names and "fast" in names
        assert "locked" not in frame  # control is on by default


# --- picking a keyword, per connection ---


def test_a_chosen_keyword_is_acknowledged_and_reaches_the_brain(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_model", "keyword": "coding"})

        ack = ws.receive_json()
        assert ack["type"] == protocol.MODEL
        assert ack["settings"]["keyword"] == "coding"
        assert ack["settings"]["model"] == models.BUILTIN_MODELS["coding"]
        assert ack["settings"]["chosen"] is True

        _speak(ws)
        assert faked_io == ["coding"]


def test_null_returns_the_connection_to_the_servers_default(faked_io):
    """Same reset semantics as a `set_voice` patch: "use yours" has to be a state a
    UI can return to, not one it can only leave."""
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_model", "keyword": "strong"})
        ws.receive_json()
        ws.send_json({"type": "set_model", "keyword": None})

        ack = ws.receive_json()
        assert ack["settings"]["keyword"] == "balanced"
        assert ack["settings"]["chosen"] is False

        _speak(ws)
        assert faked_io == [None]


def test_a_literal_model_id_is_accepted_as_a_keyword(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_model", "keyword": "vendor/experimental-1"})
        assert ws.receive_json()["settings"]["model"] == "vendor/experimental-1"


def test_a_junk_keyword_is_dropped_and_the_frame_says_so(faked_io):
    """Malformed control frames are ignored quietly everywhere in this protocol; the
    acknowledgment is what stops that being silent."""
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_model", "keyword": "not a keyword!"})
        assert ws.receive_json()["settings"]["keyword"] == "balanced"


def test_the_choice_survives_a_reconnect_with_the_same_session(faked_io):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        session_id = ws.receive_json()["session_id"]
        ws.receive_json()  # voice
        ws.receive_json()  # model
        ws.send_json({"type": "set_model", "keyword": "coding"})
        ws.receive_json()

    with client.websocket_connect(f"/ws?session_id={session_id}") as ws:
        assert ws.receive_json()["resumed"] is True
        ws.receive_json()  # voice
        assert ws.receive_json()["settings"]["keyword"] == "coding"


# --- re-pointing a keyword, per install ---


def test_a_keyword_can_be_repointed_over_the_wire(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_model", "map": {"coding": "vendor/coder-9"}})

        ack = ws.receive_json()
        by_name = {k["name"]: k for k in ack["options"]["keywords"]}
        assert by_name["coding"]["model"] == "vendor/coder-9"
        assert by_name["coding"]["overridden"] is True


def test_one_frame_can_invent_a_keyword_and_select_it(faked_io):
    """The map is applied before the keyword for exactly this: a UI that adds a
    keyword and switches to it should not need two round trips to do it."""
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json(
            {"type": "set_model", "map": {"sql": "vendor/db-tuned"}, "keyword": "sql"}
        )

        ack = ws.receive_json()
        assert ack["settings"]["keyword"] == "sql"
        assert ack["settings"]["model"] == "vendor/db-tuned"


def test_a_repointing_outlives_the_connection(faked_io):
    """Per-install, not per-session — this is the half that goes in the database."""
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_model", "map": {"writing": "vendor/prose-3"}})
        ws.receive_json()

    with client.websocket_connect("/ws") as ws:
        frame = _handshake(ws)
        by_name = {k["name"]: k for k in frame["options"]["keywords"]}
        assert by_name["writing"]["model"] == "vendor/prose-3"


def test_resetting_a_keyword_restores_its_default(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_model", "map": {"writing": "vendor/prose-3"}})
        ws.receive_json()
        ws.send_json({"type": "set_model", "map": {"writing": None}})

        by_name = {k["name"]: k for k in ws.receive_json()["options"]["keywords"]}
        assert by_name["writing"]["model"] == models.BUILTIN_MODELS["writing"]
        assert by_name["writing"]["overridden"] is False


# --- the flag ---


def test_disabled_flag_pins_the_model_and_says_so(faked_io, fresh_caches, monkeypatch):
    monkeypatch.setenv("AMBER_FEATURE_MODEL_CONTROL", "false")
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()

    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _handshake(ws)
        assert frame["locked"] is True

        ws.send_json(
            {"type": "set_model", "keyword": "coding", "map": {"coding": "vendor/x"}}
        )
        ack = ws.receive_json()
        assert ack["locked"] is True
        assert ack["settings"]["keyword"] == "balanced"  # unchanged
        by_name = {k["name"]: k for k in ack["options"]["keywords"]}
        assert by_name["coding"]["model"] == models.BUILTIN_MODELS["coding"]

        _speak(ws)
        assert faked_io == [None]
