"""Tests for the set_reminder tool — persistence and ISO time normalization."""

import pytest

import app.tools.reminders as reminders
from app.memory.store import MemoryStore


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore(":memory:")
    monkeypatch.setattr(reminders, "get_store", lambda: s)
    yield s
    s.close()


async def test_set_reminder_with_iso_time(store):
    msg = await reminders.set_reminder("call mom", "2026-06-22T17:30:00")
    assert "call mom" in msg
    rows = store.pending_reminders()
    assert len(rows) == 1
    assert rows[0]["text"] == "call mom"
    assert rows[0]["remind_at"] == "2026-06-22T17:30:00"


async def test_set_reminder_without_time(store):
    msg = await reminders.set_reminder("water the plants")
    assert "water the plants" in msg
    rows = store.pending_reminders()
    assert rows[0]["remind_at"] is None


async def test_set_reminder_rejects_an_unparseable_time(store):
    """It used to save the reminder with no time and report success, so Amber would
    say "I'll remind you at five" having stored nothing of the sort. Now the model
    is told what went wrong and can retry with a real timestamp."""
    msg = await reminders.set_reminder("stretch", "sometime tomorrow")

    assert "Error" in msg
    assert "ISO-8601" in msg
    assert store.pending_reminders() == []  # nothing saved under a false promise


async def test_set_reminder_accepts_trailing_z(store):
    await reminders.set_reminder("standup", "2026-06-22T09:00:00Z")
    assert store.pending_reminders()[0]["remind_at"] == "2026-06-22T09:00:00Z"


async def test_set_reminder_rejects_blank(store):
    msg = await reminders.set_reminder("   ")
    assert "Error" in msg
    assert store.pending_reminders() == []
