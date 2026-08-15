"""Tests for telemetry capture.

The invariant that matters more than any individual signal: **recording must never
cost a turn anything.** A full queue, a dead database, a missing event loop — all of
them are the telemetry layer's problem, never the voice loop's.
"""

import asyncio

import pytest

import app.config as config_module
from app import signals
from app.memory.store import MemoryStore
from app.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def clean_queue():
    signals.reset_for_tests()
    yield
    signals.reset_for_tests()


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def _enable(monkeypatch, on=True):
    monkeypatch.setenv("AMBER_FEATURE_SIGNALS", "true" if on else "false")
    config_module.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def restore_settings(monkeypatch):
    yield
    monkeypatch.delenv("AMBER_FEATURE_SIGNALS", raising=False)
    config_module.get_settings.cache_clear()


# --- recording ---

async def test_recording_queues_and_draining_writes(monkeypatch, store):
    _enable(monkeypatch)

    signals.record(signals.KIND_TOOL_CALL, name="web_search", ok=True, latency_ms=900)
    assert await signals.drain_once(store=store) == 1

    summary = store.event_summary("2020-01-01T00:00:00+00:00")
    assert summary[0]["name"] == "web_search"
    assert summary[0]["ok"] == 1
    assert summary[0]["avg_latency_ms"] == 900


async def test_nothing_is_recorded_when_the_flag_is_off(monkeypatch, store):
    _enable(monkeypatch, on=False)

    signals.record(signals.KIND_TOOL_CALL, name="web_search", ok=True)
    assert await signals.drain_once(store=store) == 0


async def test_a_full_queue_drops_rather_than_blocks(monkeypatch, store):
    """Losing telemetry is always better than delaying speech."""
    _enable(monkeypatch)

    for i in range(signals._QUEUE_SIZE + 50):
        signals.record(signals.KIND_TURN, name=f"turn-{i}")  # must not hang

    written = 0
    while (batch := await signals.drain_once(store=store)):
        written += batch
    assert written == signals._QUEUE_SIZE


async def test_a_broken_store_does_not_raise(monkeypatch):
    _enable(monkeypatch)

    class _Broken:
        def add_turn_events(self, rows):
            raise RuntimeError("disk on fire")

    signals.record(signals.KIND_TURN, name="voice")
    assert await signals.drain_once(store=_Broken()) == 0


async def test_long_details_are_bounded(monkeypatch, store):
    _enable(monkeypatch)

    signals.record(signals.KIND_CORRECTION, detail="x" * 5000)
    await signals.drain_once(store=store)

    row = store._conn.execute("SELECT detail FROM turn_events").fetchone()
    assert len(row["detail"]) <= 500


async def test_draining_an_empty_queue_is_a_no_op(store):
    assert await signals.drain_once(store=store) == 0


async def test_the_drain_loop_flushes_on_cancel(monkeypatch, store):
    """A clean shutdown shouldn't lose whatever hasn't been written yet."""
    _enable(monkeypatch)
    monkeypatch.setattr(signals, "get_store", lambda: store)

    task = asyncio.create_task(signals.drain_loop(interval_s=60))
    await asyncio.sleep(0)  # let it start
    signals.record(signals.KIND_TURN, name="voice")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.event_summary("2020-01-01T00:00:00+00:00")


# --- tool instrumentation ---

async def test_dispatch_records_a_successful_call(monkeypatch, store):
    _enable(monkeypatch)
    registry = ToolRegistry()
    registry.register("ok_tool", "d", {"type": "object", "properties": {}})(
        lambda: "fine"
    )

    await registry.dispatch("ok_tool", {})
    await signals.drain_once(store=store)

    row = store.event_summary("2020-01-01T00:00:00+00:00")[0]
    assert (row["kind"], row["name"], row["ok"]) == ("tool_call", "ok_tool", 1)


async def test_dispatch_records_a_raised_failure(monkeypatch, store):
    _enable(monkeypatch)
    registry = ToolRegistry()

    def boom():
        raise ValueError("nope")

    registry.register("bad_tool", "d", {"type": "object", "properties": {}})(boom)

    await registry.dispatch("bad_tool", {})
    await signals.drain_once(store=store)

    assert store.event_summary("2020-01-01T00:00:00+00:00")[0]["ok"] == 0
    # The exception *type* is kept as a hint for the review step; the full text is
    # in the log, not the database.
    row = store._conn.execute("SELECT detail FROM turn_events").fetchone()
    assert row["detail"] == "ValueError"


async def test_dispatch_records_a_returned_error_as_a_failure(monkeypatch, store):
    """Tools report failure by returning a string, not by raising — so a tool that
    keeps returning "Error: ..." has to count as failing or the telemetry lies."""
    _enable(monkeypatch)
    registry = ToolRegistry()
    registry.register("soft_fail", "d", {"type": "object", "properties": {}})(
        lambda: "Error: no active fact #9"
    )

    await registry.dispatch("soft_fail", {})
    await signals.drain_once(store=store)

    assert store.event_summary("2020-01-01T00:00:00+00:00")[0]["ok"] == 0


# --- guards on what reaches the prompt ---

async def test_an_enormous_tool_result_is_truncated():
    registry = ToolRegistry()
    registry.register("chatty", "d", {"type": "object", "properties": {}})(
        lambda: "x" * 50_000
    )

    out = await registry.dispatch("chatty", {})
    assert "[result truncated]" in out
    assert len(out) < 9000


async def test_a_huge_exception_does_not_land_in_the_prompt():
    """An unclamped repr can paste a whole library traceback into the context, where
    it's paid for on every later turn and tells the model nothing."""
    registry = ToolRegistry()

    def boom():
        raise ValueError("detail " * 5000)

    registry.register("explodes", "d", {"type": "object", "properties": {}})(boom)

    out = await registry.dispatch("explodes", {})
    assert len(out) < 500
    assert out.startswith("Error running explodes:")


# --- correction detection ---

@pytest.mark.parametrize(
    "utterance",
    [
        "No, I meant the other one",
        "that's not right",
        "Actually, make it tomorrow",
        "I said Denver",
        "you got that wrong",
        "that's not what I asked for",
    ],
)
def test_pushback_is_recognized(utterance):
    assert signals.looks_like_a_correction(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "what's the weather",
        "no problem, thanks",
        "actually useful, that",
        "tell me about Nome",
        "",
    ],
)
def test_ordinary_speech_is_not_mistaken_for_pushback(utterance):
    """Conservative on purpose: over-matching would teach the maintenance pass that
    Amber is wrong constantly, which is worse than missing some corrections."""
    assert signals.looks_like_a_correction(utterance) is False


def test_a_long_utterance_is_not_treated_as_a_correction():
    assert signals.looks_like_a_correction("no, " + "word " * 200) is False
