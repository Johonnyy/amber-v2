"""Tests for reminder firing — the scheduler, and the timezone bug it exposed.

`due_reminders` was written and indexed long before anything called it, so the
mismatch between how `remind_at` was *stored* (naive local, per the tool description)
and how "now" was *computed* (UTC) had never had a chance to be wrong out loud. It
would have been, by the length of the user's offset. Most of this file is about that.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.push as push_module
import app.reminders as reminders_module
from app import protocol
from app.config import Settings
from app.memory.store import MemoryStore
from app.reminders import fire_due, is_due, reminder_loop

DENVER = timezone(timedelta(hours=-6))


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "feature_push": True,
        "feature_memory": True,
        "feature_reminder_delivery": True,
    }
    return Settings(**{**base, **overrides})


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore(":memory:")
    monkeypatch.setattr(reminders_module, "get_store", lambda: s)
    monkeypatch.setattr(push_module, "_store", lambda: s)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def fresh_deliverer():
    push_module.reset_for_tests()
    yield
    push_module.reset_for_tests()


# --- due-ness ---------------------------------------------------------------


def test_an_offset_aware_reminder_is_compared_as_an_instant():
    now = datetime(2026, 6, 22, 23, 45, tzinfo=timezone.utc)
    assert is_due("2026-06-22T23:30:00+00:00", now)
    assert not is_due("2026-06-23T01:00:00+00:00", now)


def test_a_naive_reminder_is_read_in_the_users_zone():
    """The bug, pinned.

    A reminder written "2026-06-22T17:30:00" by a user in Denver means 23:30 UTC. Read
    as UTC — which is what a lexical comparison against `_now()` did — it would have
    fired at 17:30 UTC, six hours early, while the user was still at work.
    """
    just_before = datetime(2026, 6, 22, 23, 29, tzinfo=timezone.utc)
    just_after = datetime(2026, 6, 22, 23, 31, tzinfo=timezone.utc)

    assert not is_due("2026-06-22T17:30:00", just_before, tz=DENVER)
    assert is_due("2026-06-22T17:30:00", just_after, tz=DENVER)


def test_an_unparseable_time_never_fires():
    """Refusing to guess is the safe direction: a reminder that never fires can still
    be asked about, where one that fires at random teaches the user to ignore them."""
    assert not is_due("sometime tuesday", datetime.now(timezone.utc))
    assert not is_due(None, datetime.now(timezone.utc))


# --- firing -----------------------------------------------------------------


async def test_a_due_reminder_is_enqueued_and_marked_fired(store):
    rid = store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")

    assert await fire_due(_settings()) == 1

    pending = store.pending_pushes()
    assert len(pending) == 1
    assert pending[0]["kind"] == protocol.PUSH_REMINDER
    assert pending[0]["text"] == "call the dentist"
    assert pending[0]["ref"] == {"reminder_id": rid}
    assert store.pending_reminders()[0]["fired_at"] is not None


async def test_a_future_reminder_does_not_fire(store):
    store.add_reminder("call the dentist", "2099-01-01T10:00:00+00:00")
    assert await fire_due(_settings()) == 0
    assert store.pending_pushes() == []


async def test_a_reminder_with_no_time_never_fires(store):
    store.add_reminder("buy milk sometime")
    assert await fire_due(_settings()) == 0


async def test_a_reminder_fires_exactly_once(store):
    """`fired_at` is what stops a restart mid-pass from re-announcing everything."""
    store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")

    assert await fire_due(_settings()) == 1
    assert await fire_due(_settings()) == 0  # nothing left to fire

    assert len(store.pending_pushes()) == 1


async def test_a_fired_reminder_is_still_pending_until_completed(store):
    """Delivery is not completion. Collapsing the two would mean announcing a reminder
    silently ticked it off the user's list."""
    rid = store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    await fire_due(_settings())

    row = store.pending_reminders()[0]
    assert row["status"] == "pending"
    assert row["fired_at"] is not None

    assert store.complete_reminder(rid) is True
    assert store.pending_reminders() == []


async def test_a_completed_reminder_never_fires(store):
    rid = store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    store.complete_reminder(rid)

    assert await fire_due(_settings()) == 0


async def test_firing_is_off_when_the_feature_is_off(store):
    store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")

    assert await fire_due(_settings(feature_reminder_delivery=False)) == 0
    assert await fire_due(_settings(feature_push=False)) == 0
    assert await fire_due(_settings(feature_memory=False)) == 0
    assert store.pending_pushes() == []


async def test_a_reminder_that_cannot_be_enqueued_is_left_to_retry(store, monkeypatch):
    """Marking fired before the push is safely recorded would lose it outright."""
    store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")

    async def no_enqueue(*a, **kw):
        return None

    monkeypatch.setattr(reminders_module.push, "enqueue", no_enqueue)
    assert await fire_due(_settings()) == 0

    assert store.pending_reminders()[0]["fired_at"] is None  # will be retried


async def test_a_legacy_naive_row_still_fires_at_the_right_moment(store):
    """Rows written before normalisation landed are still in the database."""
    store._conn.execute(
        "INSERT INTO reminders (text, remind_at, status, created_at) "
        "VALUES ('legacy', '2020-01-01T17:30:00', 'pending', '2020-01-01T00:00:00+00:00')"
    )
    store._conn.commit()

    assert await fire_due(_settings()) == 1


# --- the loop ---------------------------------------------------------------


def test_due_reminders_compares_instants_not_strings(store):
    """It used to do `remind_at <= ?` in SQL against a UTC now, which is wrong the
    moment a reminder carries any offset — a 23:00 Denver reminder sorted as due five
    hours early. Nothing called it, so nothing noticed."""
    # 23:00 in Denver is 05:00 UTC the next day: not yet due at 23:30 UTC.
    store.add_reminder("late one", "2026-06-22T23:00:00-06:00")

    assert store.due_reminders("2026-06-22T23:30:00+00:00") == []
    assert len(store.due_reminders("2026-06-23T05:30:00+00:00")) == 1


def test_due_reminders_skips_fired_and_unparseable_rows(store):
    rid = store.add_reminder("done already", "2020-01-01T10:00:00+00:00")
    store.mark_reminder_fired(rid)
    store._conn.execute(
        "INSERT INTO reminders (text, remind_at, status, created_at) "
        "VALUES ('nonsense', 'next tuesday', 'pending', '2020-01-01T00:00:00+00:00')"
    )
    store._conn.commit()

    assert store.due_reminders() == []


async def test_the_loop_waits_out_its_startup_delay(store):
    store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    task = asyncio.create_task(
        reminder_loop(_settings(reminder_startup_delay_s=60.0))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.pending_pushes() == []


async def test_the_loop_fires_and_then_cancels_cleanly(store):
    store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    task = asyncio.create_task(
        reminder_loop(_settings(reminder_startup_delay_s=0.0))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(store.pending_pushes()) == 1
