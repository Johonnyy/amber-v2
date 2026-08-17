"""Tests for the push transport — the outbox, the deliverer, and the ack.

The case that justifies this module existing at all is `test_a_push_survives_having_no
_one_to_deliver_to`: everything else in the protocol may be dropped when nobody is
listening, and a reminder may not.
"""

import asyncio

import pytest

import app.push as push_module
from app import protocol
from app.memory.store import MemoryStore
from app.push import Deliverer


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


class Sink:
    """A bound connection. ``idle`` and ``fail`` are what the tests vary."""

    def __init__(self, *, idle=True, fail=False):
        self.frames: list[dict] = []
        self._idle = idle
        self._fail = fail

    async def send(self, frame: dict) -> None:
        if self._fail:
            raise RuntimeError("socket is gone")
        self.frames.append(frame)

    def is_idle(self) -> bool:
        return self._idle


# --- the outbox -------------------------------------------------------------


async def test_enqueue_records_a_push_and_returns_its_id(store):
    push_id = await push_module.enqueue(
        protocol.PUSH_REMINDER, "call the dentist", ref={"reminder_id": 12}
    )

    assert push_id and push_id.startswith("p_")
    rows = store.pending_pushes()
    assert len(rows) == 1
    assert rows[0]["text"] == "call the dentist"
    assert rows[0]["ref"] == {"reminder_id": 12}


async def test_enqueue_ignores_an_empty_message(store):
    assert await push_module.enqueue(protocol.PUSH_NOTICE, "   ") is None
    assert store.pending_pushes() == []


# --- delivery ---------------------------------------------------------------


async def test_a_push_reaches_a_bound_idle_sink(store):
    await push_module.enqueue(protocol.PUSH_NOTICE, "build finished")
    deliverer = Deliverer()
    sink = Sink()
    deliverer.bind("s1", sink.send, sink.is_idle)

    assert await deliverer.flush() == 1

    assert len(sink.frames) == 1
    frame = sink.frames[0]
    assert frame["type"] == protocol.PUSH
    assert frame["kind"] == protocol.PUSH_NOTICE
    assert frame["text"] == "build finished"
    # Settled, so it is not announced twice on the next pass.
    assert store.pending_pushes() == []


async def test_a_push_survives_having_no_one_to_deliver_to(store):
    """The whole reason the outbox is a table and not a queue.

    A reminder due while the socket is down must arrive when a client comes back, not
    disappear because nothing happened to be listening at 17:30.
    """
    await push_module.enqueue(protocol.PUSH_REMINDER, "call the dentist")
    deliverer = Deliverer()

    assert await deliverer.flush() == 0
    assert len(store.pending_pushes()) == 1  # still waiting, not dropped

    # ...and now someone connects.
    sink = Sink()
    deliverer.bind("s1", sink.send, sink.is_idle)
    assert await deliverer.flush() == 1
    assert sink.frames[0]["text"] == "call the dentist"


async def test_delivery_waits_for_a_connection_that_is_mid_turn(store):
    """A push written between an ``audio_chunk`` and its bytes would corrupt the audio
    stream for every client, so a busy connection is skipped rather than written to."""
    await push_module.enqueue(protocol.PUSH_NOTICE, "later")
    deliverer = Deliverer()
    busy = Sink(idle=False)
    deliverer.bind("s1", busy.send, busy.is_idle)

    assert await deliverer.flush() == 0
    assert busy.frames == []
    assert len(store.pending_pushes()) == 1

    busy._idle = True
    assert await deliverer.flush() == 1
    assert len(busy.frames) == 1


async def test_a_failing_sink_is_unbound_and_the_push_stays_pending(store):
    """Once starlette latches a closed socket every later send raises, including on
    paths that have nothing to do with pushes — so a raising sink goes immediately."""
    await push_module.enqueue(protocol.PUSH_NOTICE, "hello")
    deliverer = Deliverer()
    dead = Sink(fail=True)
    deliverer.bind("s1", dead.send, dead.is_idle)

    assert await deliverer.flush() == 0

    assert deliverer.live() == 0
    assert len(store.pending_pushes()) == 1


async def test_one_live_sink_is_enough_when_another_is_dead(store):
    await push_module.enqueue(protocol.PUSH_NOTICE, "hello")
    deliverer = Deliverer()
    dead, good = Sink(fail=True), Sink()
    deliverer.bind("dead", dead.send, dead.is_idle)
    deliverer.bind("good", good.send, good.is_idle)

    assert await deliverer.flush() == 1

    assert len(good.frames) == 1
    assert deliverer.live() == 1  # the dead one dropped itself, the good one stayed


async def test_flush_can_target_one_session(store):
    """The reconnect case: deliver the backlog to the client that just arrived without
    also broadcasting it to everyone who was already here."""
    await push_module.enqueue(protocol.PUSH_NOTICE, "welcome back")
    deliverer = Deliverer()
    arriving, other = Sink(), Sink()
    deliverer.bind("arriving", arriving.send, arriving.is_idle)
    deliverer.bind("other", other.send, other.is_idle)

    assert await deliverer.flush(session_id="arriving") == 1

    assert len(arriving.frames) == 1
    assert other.frames == []


async def test_unbind_is_idempotent(store):
    deliverer = Deliverer()
    sink = Sink()
    deliverer.bind("s1", sink.send, sink.is_idle)
    deliverer.unbind("s1")
    deliverer.unbind("s1")
    assert deliverer.live() == 0


async def test_delivery_is_batched(store, monkeypatch):
    """A backlog arrives in digestible batches rather than all at once."""
    import app.config as config_module

    monkeypatch.setenv("AMBER_PUSH_BATCH", "2")
    config_module.get_settings.cache_clear()
    try:
        for i in range(5):
            await push_module.enqueue(protocol.PUSH_NOTICE, f"note {i}")
        deliverer = Deliverer()
        sink = Sink()
        deliverer.bind("s1", sink.send, sink.is_idle)

        assert await deliverer.flush() == 2
        assert len(store.pending_pushes()) == 3
    finally:
        config_module.get_settings.cache_clear()


# --- acknowledgment ---------------------------------------------------------


async def test_ack_complete_finishes_the_reminder_behind_the_push(store):
    """The standing rule: a thing done by tapping and one done by asking are the same
    row. ``complete`` lands on the same store function the tool calls."""
    reminder_id = store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    push_id = await push_module.enqueue(
        protocol.PUSH_REMINDER, "call the dentist", ref={"reminder_id": reminder_id}
    )

    await push_module.acknowledge(
        {"id": push_id, "action": protocol.ACK_COMPLETE}
    )

    assert store.pending_reminders() == []  # completed, not merely dismissed


async def test_ack_seen_settles_the_push_without_touching_the_reminder(store):
    reminder_id = store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    push_id = await push_module.enqueue(
        protocol.PUSH_REMINDER, "call the dentist", ref={"reminder_id": reminder_id}
    )

    await push_module.acknowledge({"id": push_id, "action": protocol.ACK_SEEN})

    assert store.pending_pushes() == []  # the push is settled
    assert len(store.pending_reminders()) == 1  # the task itself is not


async def test_a_non_reminder_push_cannot_complete_a_reminder(store):
    """`POST /push` refuses to let a caller claim `kind: reminder`, but passes `ref`
    through as given — so without a kind check an authenticated peer could post an
    innocuous notice carrying someone else's `reminder_id` and have one tap tick it
    off. The kind is the part Amber controls; that is what is trusted."""
    reminder_id = store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    push_id = await push_module.enqueue(
        protocol.PUSH_NOTICE, "build finished", ref={"reminder_id": reminder_id}
    )

    await push_module.acknowledge({"id": push_id, "action": protocol.ACK_COMPLETE})

    assert len(store.pending_reminders()) == 1  # untouched


async def test_a_boolean_reminder_id_does_not_complete_reminder_one(store):
    """`bool` is an `int` in Python, so `{"reminder_id": true}` would otherwise pass
    an isinstance check and complete reminder #1."""
    store.add_reminder("call the dentist", "2020-01-01T10:00:00+00:00")
    push_id = await push_module.enqueue(
        protocol.PUSH_REMINDER, "call the dentist", ref={"reminder_id": True}
    )

    await push_module.acknowledge({"id": push_id, "action": protocol.ACK_COMPLETE})

    assert len(store.pending_reminders()) == 1


async def test_a_stale_connection_cannot_unbind_the_one_that_replaced_it(store):
    """A session outlives its socket, so a reconnect binds a new sink while the old
    handler may still be in its `finally`. Letting that one win would leave a client
    that is plainly connected silently receiving nothing."""
    deliverer = Deliverer()
    old, new = object(), object()
    old_sink, new_sink = Sink(), Sink()

    deliverer.bind("s1", old_sink.send, old_sink.is_idle, token=old)
    deliverer.bind("s1", new_sink.send, new_sink.is_idle, token=new)
    deliverer.unbind("s1", token=old)  # the old handler finally unwinding

    assert deliverer.live() == 1
    await push_module.enqueue(protocol.PUSH_NOTICE, "still here")
    assert await deliverer.flush() == 1
    assert len(new_sink.frames) == 1
    assert old_sink.frames == []


async def test_a_connection_still_unbinds_itself(store):
    deliverer = Deliverer()
    mine = object()
    sink = Sink()
    deliverer.bind("s1", sink.send, sink.is_idle, token=mine)
    deliverer.unbind("s1", token=mine)
    assert deliverer.live() == 0


async def test_a_malformed_ack_is_ignored(store):
    await push_module.acknowledge({"action": protocol.ACK_COMPLETE})
    await push_module.acknowledge({"id": 12})
    await push_module.acknowledge({"id": "p_nope", "action": protocol.ACK_COMPLETE})


# --- the loop ---------------------------------------------------------------


async def test_the_loop_cancels_cleanly(store):
    deliverer = Deliverer()
    task = asyncio.create_task(deliverer.deliver_loop(interval_s=60))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_nudge_wakes_the_loop_without_waiting_out_the_interval(store):
    """A reminder that fires should not sit for the whole polling interval first."""
    deliverer = Deliverer()
    sink = Sink()
    deliverer.bind("s1", sink.send, sink.is_idle)
    task = asyncio.create_task(deliverer.deliver_loop(interval_s=60))
    await asyncio.sleep(0.01)

    await push_module.enqueue(protocol.PUSH_REMINDER, "wake up")
    deliverer.nudge()
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [f["text"] for f in sink.frames] == ["wake up"]


async def test_expiry_retires_pushes_nobody_collected(store):
    await push_module.enqueue(protocol.PUSH_NOTICE, "ancient history")
    store._conn.execute(
        "UPDATE push_outbox SET created_at = '2020-01-01T00:00:00+00:00'"
    )
    store._conn.commit()

    assert store.expire_pushes(14.0) == 1
    assert store.pending_pushes() == []
