"""Tests for the OpenClaw bridge — availability gating, request shape, parsing."""

import httpx
import pytest

import app.tools.openclaw as openclaw
from app.config import Settings


def _settings(**over):
    base = dict(openclaw_url="https://claw.example", openclaw_token="", openclaw_timeout_s=5.0)
    base.update(over)
    return Settings(_env_file=None, **base)


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data, capture):
        self._data = data
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self._capture["url"] = url
        self._capture["json"] = json
        self._capture["headers"] = headers
        return _FakeResponse(self._data)


def test_available_predicate_follows_url(monkeypatch):
    monkeypatch.setattr(openclaw, "get_settings", lambda: _settings(openclaw_url=""))
    assert openclaw._configured() is False
    monkeypatch.setattr(openclaw, "get_settings", lambda: _settings())
    assert openclaw._configured() is True


async def test_delegate_posts_task_and_returns_result(monkeypatch):
    capture = {}
    monkeypatch.setattr(
        openclaw.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient({"result": "Booked it."}, capture),
    )
    monkeypatch.setattr(
        openclaw, "get_settings", lambda: _settings(openclaw_token="secret")
    )

    out = await openclaw.delegate_to_openclaw("book a table for two")
    assert out == "Booked it."
    assert capture["url"] == "https://claw.example/task"
    assert capture["json"] == {"task": "book a table for two"}
    assert capture["headers"]["Authorization"] == "Bearer secret"


async def test_delegate_no_token_sends_no_auth_header(monkeypatch):
    capture = {}
    monkeypatch.setattr(
        openclaw.httpx, "AsyncClient", lambda *a, **k: _FakeClient({"output": "ok"}, capture)
    )
    monkeypatch.setattr(openclaw, "get_settings", lambda: _settings())

    await openclaw.delegate_to_openclaw("do a thing")
    assert "Authorization" not in capture["headers"]


async def test_delegate_when_not_configured(monkeypatch):
    monkeypatch.setattr(openclaw, "get_settings", lambda: _settings(openclaw_url=""))
    out = await openclaw.delegate_to_openclaw("anything")
    assert "isn't configured" in out


async def test_delegate_empty_task(monkeypatch):
    monkeypatch.setattr(openclaw, "get_settings", lambda: _settings())
    out = await openclaw.delegate_to_openclaw("   ")
    assert "Error" in out


async def test_delegate_http_error_degrades(monkeypatch):
    class _ErrClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        openclaw.httpx, "AsyncClient", lambda *a, **k: _ErrClient({}, {})
    )
    monkeypatch.setattr(openclaw, "get_settings", lambda: _settings())

    out = await openclaw.delegate_to_openclaw("do work")
    assert "Couldn't reach OpenClaw" in out
