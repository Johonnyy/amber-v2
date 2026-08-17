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
    """A naive time is read as local and stored offset-aware.

    It used to be stored verbatim, which was invisible until reminders began firing:
    every other timestamp in the database is UTC, so comparing a naive local string
    against "now" mis-fired by the length of the user's offset — six hours early in
    Denver. The tests here pin the *normalised* form because that ambiguity was the bug.
    """
    msg = await reminders.set_reminder("call mom", "2026-06-22T17:30:00")
    assert "call mom" in msg
    rows = store.pending_reminders()
    assert len(rows) == 1
    assert rows[0]["text"] == "call mom"
    assert rows[0]["remind_at"] == "2026-06-22T17:30:00+00:00"


async def test_a_naive_time_is_read_in_the_configured_zone(store, monkeypatch):
    """The zone the model was *told* to use is the one used to interpret its answer.

    Skipped where the IANA database isn't installed (a bare Windows box without
    ``tzdata``), which is the same condition `runtime_context._resolve_tz` degrades to
    UTC for. The deploy target is Linux, where the zones exist.
    """
    pytest.importorskip("zoneinfo")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo("America/Denver")
    except (ZoneInfoNotFoundError, OSError):
        pytest.skip("No IANA timezone database on this machine")

    import app.config as config_module

    monkeypatch.setenv("AMBER_TIMEZONE", "America/Denver")
    config_module.get_settings.cache_clear()
    try:
        await reminders.set_reminder("call mom", "2026-06-22T17:30:00")
    finally:
        config_module.get_settings.cache_clear()

    # 17:30 in Denver (MDT, UTC-6) is 23:30 UTC — the same instant, not the same digits.
    # Stored verbatim, this reminder would have fired six hours early.
    assert store.pending_reminders()[0]["remind_at"] == "2026-06-22T17:30:00-06:00"


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
    """``Z`` is still accepted, and now lands in the same shape as everything else.

    Storing it intact was half of what made the column ambiguous: two conventions in
    one place, with nothing to say which a given row followed.
    """
    await reminders.set_reminder("standup", "2026-06-22T09:00:00Z")
    assert store.pending_reminders()[0]["remind_at"] == "2026-06-22T09:00:00+00:00"


async def test_set_reminder_echoes_the_time_in_local_words(store):
    """What comes back is spoken aloud, so it must not be the UTC that was stored."""
    msg = await reminders.set_reminder("call mom", "2026-06-22T17:30:00")
    assert "5:30 PM" in msg
    assert "+00:00" not in msg


async def test_set_reminder_rejects_blank(store):
    msg = await reminders.set_reminder("   ")
    assert "Error" in msg
    assert store.pending_reminders() == []
