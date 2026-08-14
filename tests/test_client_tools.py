"""Tests for client-declared tools — registration, schemas, call/result plumbing."""

import asyncio

import pytest

import app.client_tools as client_tools_mod
from app.client_tools import ClientTools
from app.config import Settings


def _settings(**over):
    base = dict(max_client_tools=16, client_tool_timeout_s=5.0)
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def patched_settings(monkeypatch):
    monkeypatch.setattr(client_tools_mod, "get_settings", lambda: _settings())


def test_register_prefixes_and_sanitizes(patched_settings):
    ct = ClientTools()
    names = ct.register(
        [
            {"name": "display text", "description": "show text", "input_schema": {}},
            {"name": "client_play_sound", "description": "beep"},
        ]
    )
    # Spaces sanitized to underscores; the client_ prefix is added once, not twice.
    assert names == ["client_display_text", "client_play_sound"]
    schemas = ct.schemas()
    assert {s["name"] for s in schemas} == {"client_display_text", "client_play_sound"}
    # A missing input_schema gets a safe default object schema.
    by_name = {s["name"]: s for s in schemas}
    assert by_name["client_play_sound"]["input_schema"] == {
        "type": "object",
        "properties": {},
    }


def test_register_skips_invalid_entries(patched_settings):
    ct = ClientTools()
    names = ct.register(
        ["not a dict", {"description": "no name"}, {"name": "  "}, {"name": "ok"}]
    )
    assert names == ["client_ok"]


def test_register_caps_at_max(monkeypatch):
    monkeypatch.setattr(
        client_tools_mod, "get_settings", lambda: _settings(max_client_tools=2)
    )
    ct = ClientTools()
    names = ct.register(
        [{"name": f"t{i}"} for i in range(5)]
    )
    assert len(names) == 2


def test_register_non_list_is_ignored(patched_settings):
    ct = ClientTools()
    assert ct.register({"name": "x"}) == []
    assert ct.schemas() == []


def test_handles(patched_settings):
    ct = ClientTools()
    ct.register([{"name": "display_text"}])
    assert ct.handles("client_display_text") is True
    assert ct.handles("web_search") is False


async def test_call_sends_frame_and_returns_result(patched_settings):
    ct = ClientTools()
    ct.register([{"name": "display_text"}])

    sent = []

    async def send(frame):
        sent.append(frame)

    ct.bind(send)

    async def run():
        return await ct.call("client_display_text", {"text": "hi"})

    task = asyncio.create_task(run())
    await asyncio.sleep(0)  # let call() send its frame and start awaiting

    assert len(sent) == 1
    frame = sent[0]
    assert frame["type"] == "tool_call"
    assert frame["name"] == "client_display_text"
    assert frame["input"] == {"text": "hi"}
    call_id = frame["id"]

    ct.resolve(call_id, "shown")
    assert await task == "shown"


async def test_call_unknown_tool(patched_settings):
    ct = ClientTools()
    out = await ct.call("client_nope", {})
    assert "not available" in out


async def test_call_no_client_connected(patched_settings):
    ct = ClientTools()
    ct.register([{"name": "display_text"}])
    out = await ct.call("client_display_text", {})  # never bound
    assert "isn't connected" in out


async def test_call_timeout(monkeypatch):
    monkeypatch.setattr(
        client_tools_mod, "get_settings", lambda: _settings(client_tool_timeout_s=0.05)
    )
    ct = ClientTools()
    ct.register([{"name": "display_text"}])

    async def send(frame):
        pass

    ct.bind(send)
    out = await ct.call("client_display_text", {})
    assert "in time" in out


async def test_resolve_error_flag_marks_result(patched_settings):
    ct = ClientTools()
    ct.register([{"name": "display_text"}])

    async def send(frame):
        pass

    ct.bind(send)
    task = asyncio.create_task(ct.call("client_display_text", {}))
    await asyncio.sleep(0)
    # Grab the pending id by resolving via the only pending call.
    call_id = next(iter(ct._pending))
    ct.resolve(call_id, "no screen here", is_error=True)
    result = await task
    assert result.startswith("Error from client:")
    assert "no screen here" in result


def test_resolve_unknown_id_is_noop(patched_settings):
    ct = ClientTools()
    ct.resolve("does-not-exist", "x")  # must not raise


async def test_unbind_fails_pending_calls(patched_settings):
    ct = ClientTools()
    ct.register([{"name": "display_text"}])

    async def send(frame):
        pass

    ct.bind(send)
    task = asyncio.create_task(ct.call("client_display_text", {}))
    await asyncio.sleep(0)
    ct.unbind()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Specs persist across the disconnect for a future reconnect.
    assert ct.handles("client_display_text") is True
