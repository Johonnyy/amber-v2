"""Tests for the update_server tool — gating, success, failure, timeout."""

import asyncio

import pytest

import app.tools.update as update
from app.config import Settings


def _settings(**over):
    base = dict(update_command="bash /opt/amber/deploy/update.sh", update_timeout_s=5.0)
    base.update(over)
    return Settings(_env_file=None, **base)


class _FakeProc:
    def __init__(self, output=b"", returncode=0, hang=False):
        self._output = output
        self.returncode = returncode
        self._hang = hang

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(60)  # never returns within the test timeout
        return self._output, b""


def test_available_predicate_follows_command(monkeypatch):
    monkeypatch.setattr(update, "get_settings", lambda: _settings(update_command=""))
    assert update._configured() is False
    monkeypatch.setattr(update, "get_settings", lambda: _settings())
    assert update._configured() is True


async def test_update_runs_command_and_reports_success(monkeypatch):
    captured = {}

    async def fake_shell(command, **kwargs):
        captured["command"] = command
        return _FakeProc(output=b"ok updated\n", returncode=0)

    monkeypatch.setattr(update, "get_settings", lambda: _settings())
    monkeypatch.setattr(update.asyncio, "create_subprocess_shell", fake_shell)

    out = await update.update_server()
    assert "successfully" in out
    assert "ok updated" in out
    assert captured["command"] == "bash /opt/amber/deploy/update.sh"


async def test_update_reports_failure_with_output(monkeypatch):
    async def fake_shell(command, **kwargs):
        return _FakeProc(output=b"boom\n", returncode=3)

    monkeypatch.setattr(update, "get_settings", lambda: _settings())
    monkeypatch.setattr(update.asyncio, "create_subprocess_shell", fake_shell)

    out = await update.update_server()
    assert "failed" in out
    assert "exit 3" in out
    assert "boom" in out


async def test_update_timeout_reports_started(monkeypatch):
    async def fake_shell(command, **kwargs):
        return _FakeProc(hang=True)

    monkeypatch.setattr(
        update, "get_settings", lambda: _settings(update_timeout_s=0.05)
    )
    monkeypatch.setattr(update.asyncio, "create_subprocess_shell", fake_shell)

    out = await update.update_server()
    assert "started" in out.lower()


async def test_update_not_configured(monkeypatch):
    monkeypatch.setattr(update, "get_settings", lambda: _settings(update_command=""))
    out = await update.update_server()
    assert "isn't configured" in out
