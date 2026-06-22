"""Tests for the tool registry — schema export, availability, and safe dispatch."""

import pytest

from app.tools.registry import ToolRegistry


def _schema():
    return {"type": "object", "properties": {"x": {"type": "string"}}}


def test_register_and_export_schema():
    reg = ToolRegistry()

    @reg.register("echo", "echoes x", _schema())
    def echo(x):
        return f"got {x}"

    assert reg.names() == ["echo"]
    schemas = reg.schemas()
    assert schemas == [
        {"name": "echo", "description": "echoes x", "input_schema": _schema()}
    ]


def test_duplicate_registration_raises():
    reg = ToolRegistry()
    reg.register("dup", "first", _schema())(lambda x=None: "a")
    with pytest.raises(ValueError):
        reg.register("dup", "second", _schema())(lambda x=None: "b")


async def test_dispatch_runs_sync_tool():
    reg = ToolRegistry()

    @reg.register("echo", "d", _schema())
    def echo(x):
        return f"got {x}"

    assert await reg.dispatch("echo", {"x": "hi"}) == "got hi"


async def test_dispatch_runs_async_tool_and_stringifies():
    reg = ToolRegistry()

    @reg.register("count", "d", {"type": "object", "properties": {}})
    async def count():
        return 42  # non-string return is coerced to a string

    assert await reg.dispatch("count", None) == "42"


async def test_dispatch_unknown_tool_returns_error_string():
    reg = ToolRegistry()
    result = await reg.dispatch("nope", {})
    assert "not available" in result


async def test_dispatch_swallows_tool_exception():
    reg = ToolRegistry()

    @reg.register("boom", "d", {"type": "object", "properties": {}})
    def boom():
        raise RuntimeError("kaboom")

    result = await reg.dispatch("boom", {})
    assert "Error running boom" in result
    assert "kaboom" in result


async def test_dispatch_reraises_cancellation():
    import asyncio

    reg = ToolRegistry()

    @reg.register("cancel", "d", {"type": "object", "properties": {}})
    async def cancel():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await reg.dispatch("cancel", {})


def test_unavailable_tool_hidden_from_schemas():
    reg = ToolRegistry()
    enabled = {"on": False}

    reg.register(
        "gated", "d", _schema(), available=lambda: enabled["on"]
    )(lambda x=None: "ok")

    assert reg.schemas() == []  # hidden while unavailable
    assert reg.names() == ["gated"]  # still registered
    enabled["on"] = True
    assert [s["name"] for s in reg.schemas()] == ["gated"]


async def test_unavailable_tool_refuses_dispatch():
    reg = ToolRegistry()
    reg.register("gated", "d", _schema(), available=lambda: False)(lambda x=None: "ok")
    assert "not available" in await reg.dispatch("gated", {"x": "1"})
