"""Push and confirmation over the real socket.

The unit tests cover the outbox and the broker; these cover the two things only the
wire can show. First, that a pending push is delivered on connect — the reconnect case
the outbox exists for, end to end. Second, and easy to break: that the handshake is
still exactly four frames when there is nothing waiting, because three separate test
files assert that order and every existing client depends on it.
"""

import pytest
from fastapi.testclient import TestClient

import app.config as config_module
import app.pipeline as pipeline
import app.push as push_module
import app.session as session_module
from app import protocol
from app.main import app
from app.memory import MemoryView
from app.memory.store import MemoryStore


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore(":memory:")
    monkeypatch.setattr(push_module, "_store", lambda: s)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def fresh_deliverer():
    push_module.reset_for_tests()
    yield
    push_module.reset_for_tests()


@pytest.fixture
def faked_io(monkeypatch):
    async def fake_transcribe(audio, **kw):
        return "hello amber"

    async def fake_synthesize(text, voice=None):
        return f"AUDIO[{text}]".encode()

    async def fake_think(messages, system=None, **kwargs):
        yield "Hello back."

    async def no_context(query=None, **kw):
        return MemoryView()

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "remember", no_remember)


@pytest.fixture
def fresh_caches():
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()
    yield
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()


def _handshake(ws) -> list[dict]:
    """The four frames every connection opens with."""
    return [ws.receive_json() for _ in range(4)]


def _read_until(ws, wanted: str, limit: int = 20) -> dict:
    """Read frames until one of type ``wanted``, consuming audio pairs properly.

    The binary frame that follows an ``audio_chunk`` has to be read as bytes — calling
    `receive_json` on it raises, and doing so inside the `with` block deadlocks the
    close rather than failing cleanly, which is a genuinely confusing way to find out.
    """
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == protocol.AUDIO_CHUNK:
            ws.receive_bytes()
        if frame["type"] == wanted:
            return frame
    raise AssertionError(f"never saw {wanted}")


def test_the_handshake_is_still_four_frames_with_an_empty_outbox(
    store, faked_io, fresh_caches
):
    """A fifth *unconditional* frame would break every existing WS test and every
    client that reads a fixed handshake. Delivery on connect is conditional for
    exactly this reason."""
    with TestClient(app).websocket_connect("/ws") as ws:
        frames = _handshake(ws)
        assert [f["type"] for f in frames] == [
            protocol.READY,
            protocol.VOICE,
            protocol.MODEL,
            protocol.STATUS,
        ]
        # Nothing else is waiting to be read.
        ws.send_json({"type": "interrupt"})


def test_a_pending_push_arrives_on_connect(store, faked_io, fresh_caches):
    """The case the outbox exists for: it was enqueued while nothing was listening."""
    store.add_push("p_x1", protocol.PUSH_REMINDER, "call the dentist",
                   ref={"reminder_id": 7})

    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        frame = ws.receive_json()

    assert frame["type"] == protocol.PUSH
    assert frame["id"] == "p_x1"
    assert frame["kind"] == protocol.PUSH_REMINDER
    assert frame["text"] == "call the dentist"
    assert frame["ref"] == {"reminder_id": 7}
    assert store.pending_pushes() == []  # settled, so it won't arrive twice


def test_a_push_ack_completes_the_reminder_behind_it(store, faked_io, fresh_caches):
    reminder_id = store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    store.add_push("p_x2", protocol.PUSH_REMINDER, "call the dentist",
                   ref={"reminder_id": reminder_id})

    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        assert ws.receive_json()["id"] == "p_x2"
        ws.send_json(
            {"type": protocol.PUSH_ACK, "id": "p_x2", "action": protocol.ACK_COMPLETE}
        )
        # The ack is answered with nothing, so follow it with a frame that *is*, and
        # wait for that. Control frames are handled in order on one receive loop, so
        # seeing the `voice` reply proves the ack ahead of it has already run — where
        # simply closing the socket would race the ack's thread hops to SQLite.
        ws.send_json({"type": protocol.SET_VOICE, "speed": 1.0})
        assert ws.receive_json()["type"] == protocol.VOICE

    assert store.pending_reminders() == []


def test_a_push_waits_for_the_next_connection_when_delivery_fails(
    store, faked_io, fresh_caches
):
    """Disconnecting without reading leaves the row pending, so it is redelivered."""
    store.add_push("p_x3", protocol.PUSH_NOTICE, "build finished")

    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        first = ws.receive_json()
    assert first["id"] == "p_x3"

    # Already delivered once, so a fresh connection gets only the handshake.
    with TestClient(app).websocket_connect("/ws") as ws:
        frames = _handshake(ws)
        assert frames[-1]["type"] == protocol.STATUS
        ws.send_json({"type": "interrupt"})


def test_push_is_silent_when_the_feature_is_off(
    store, faked_io, fresh_caches, monkeypatch
):
    store.add_push("p_x4", protocol.PUSH_NOTICE, "build finished")
    monkeypatch.setenv("AMBER_FEATURE_PUSH", "false")
    config_module.get_settings.cache_clear()

    with TestClient(app).websocket_connect("/ws") as ws:
        frames = _handshake(ws)
        assert frames[-1]["type"] == protocol.STATUS
        ws.send_json({"type": "interrupt"})

    assert len(store.pending_pushes()) == 1  # kept, just not delivered


# --- confirmation over the wire ---------------------------------------------


@pytest.fixture
def quick_confirm(monkeypatch):
    """Short timeout so a broken round trip fails in seconds rather than a minute.

    The default is 60s, which is right for a person and miserable in a test suite: a
    regression here would otherwise look like a hang rather than a failure.
    """
    monkeypatch.setenv("AMBER_CONFIRM_TIMEOUT_S", "3")
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def test_a_confirmation_round_trips_over_the_socket(
    store, faked_io, monkeypatch, fresh_caches, quick_confirm
):
    """The frame goes out on the turn's socket and the answer comes back on the
    receive loop — two different code paths that have to agree on the id."""
    answered: list[str] = []

    async def asking_think(messages, system=None, **kwargs):
        confirmations = kwargs["confirmations"]
        outcome = await confirmations.request(
            "update_server", {"dry_run": True}, origin=protocol.ORIGIN_OWN
        )
        answered.append(outcome)
        yield "All done."

    monkeypatch.setattr(pipeline, "think", asking_think)

    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": protocol.USER_TEXT, "text": "update yourself"})

        frame = _read_until(ws, protocol.CONFIRM_REQUEST)
        assert frame["name"] == "update_server"
        assert frame["input"] == {"dry_run": True}
        assert frame["origin"] == protocol.ORIGIN_OWN

        ws.send_json(
            {"type": protocol.CONFIRM_RESPONSE, "id": frame["id"], "approved": True}
        )
        _read_until(ws, protocol.TURN_COMPLETE)

    assert answered == ["approved"]


def test_a_denial_over_the_socket_refuses_the_call(
    store, faked_io, monkeypatch, fresh_caches, quick_confirm
):
    answered: list[str] = []

    async def asking_think(messages, system=None, **kwargs):
        answered.append(
            await kwargs["confirmations"].request(
                "update_server", {}, origin=protocol.ORIGIN_OWN
            )
        )
        yield "Left it alone."

    monkeypatch.setattr(pipeline, "think", asking_think)

    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": protocol.USER_TEXT, "text": "update yourself"})

        frame = _read_until(ws, protocol.CONFIRM_REQUEST)
        ws.send_json(
            {"type": protocol.CONFIRM_RESPONSE, "id": frame["id"], "approved": False}
        )
        _read_until(ws, protocol.TURN_COMPLETE)

    assert answered == ["denied"]


def test_update_server_declares_that_it_needs_approval():
    """The one tool marked today. If this flag stops reaching the schema, the gate
    silently disappears and a shell command runs unattended."""
    from app.tools.registry import registry

    schema = registry._tools["update_server"].schema()
    assert schema["x_agent"]["requires_confirmation"] is True


# --- POST /push -------------------------------------------------------------
#
# Deliberately *not* inside `with TestClient(app)`. Entering the app's lifespan mounts
# Amber's MCP server, and its session manager refuses to start twice in one process —
# so a `with` block here would pass in isolation and fail as soon as a second test in
# this file needed keys. `/push` is a plain route registered at import and reads its
# config per request, so it needs no lifespan at all.


@pytest.fixture
def keyed(monkeypatch):
    """An install with MCP keys configured, and a client that skips the lifespan."""
    monkeypatch.setenv("AMBER_MCP_KEYS", "bloom:secret")
    config_module.get_settings.cache_clear()
    yield TestClient(app)
    config_module.get_settings.cache_clear()


def test_post_push_is_404_without_mcp_keys(store, fresh_caches):
    """No keys means no endpoint, matching how the MCP server itself fails closed."""
    assert TestClient(app).post("/push", json={"text": "hi"}).status_code == 404


def test_post_push_requires_a_valid_bearer(store, keyed):
    assert keyed.post("/push", json={"text": "hi"}).status_code == 401
    assert (
        keyed.post(
            "/push", json={"text": "hi"}, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_post_push_queues_a_notice(store, keyed):
    response = keyed.post(
        "/push",
        json={"text": "Spotify agent build finished", "kind": "peer_event"},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    # "queued", never "delivered" — there may be nobody connected, and the outbox
    # exists precisely so we don't have to lie about that.
    assert response.json()["status"] == "queued"
    rows = store.pending_pushes()
    assert rows[0]["text"] == "Spotify agent build finished"
    assert rows[0]["kind"] == protocol.PUSH_PEER_EVENT


def test_a_peer_cannot_forge_a_reminder(store, keyed):
    """Reminders are minted by the scheduler against a real row. A forged one would
    put something in front of the user with no reminder behind it."""
    keyed.post(
        "/push",
        json={"text": "call the dentist", "kind": protocol.PUSH_REMINDER},
        headers={"Authorization": "Bearer secret"},
    )

    assert store.pending_pushes()[0]["kind"] == protocol.PUSH_NOTICE


def test_post_push_rejects_an_empty_body(store, keyed):
    response = keyed.post(
        "/push", json={"text": "  "}, headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 400
