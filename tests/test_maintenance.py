"""Tests for the maintenance pass — the loop that makes Amber better unattended.

Two properties matter more than any individual step. It must be **safe**: bounded,
idempotent, and unable to take anything down when a step fails. And it must be
**bounded in blast radius**: a confused model gets to mangle a fixed number of facts
and no more, and can never touch a fact it wasn't shown.

The LLM is faked throughout — the deterministic steps don't need one at all, which
is exactly why they run first.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.maintenance import MaintenanceReport, maintenance_loop, run_maintenance
from app.memory.store import MemoryStore

_LONG_AGO = "2020-01-01T00:00:00+00:00"


class FakeRunner:
    """Stands in for agent_runtime's AgentRunner; replays canned responses in turn."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def stream(self, messages, *, system=None, conversation_id=None, depth=0):
        self.calls.append({"payload": messages, "system": system})
        text = self._responses.pop(0) if self._responses else "[]"

        async def gen():
            half = len(text) // 2
            yield text[:half]
            yield text[half:]

        return gen()


def _settings(**over):
    base = dict(
        feature_memory=True,
        fact_short_ttl_days=30.0,
        fact_session_ttl_hours=12.0,
        fact_promote_uses=3,
        conversation_keep_days=180.0,
        signals_keep_days=30.0,
        maintenance_max_facts=60,
        maintenance_max_changes=20,
        maintenance_max_reflections=3,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def _backdate(store, fact_id, iso=_LONG_AGO):
    store._conn.execute(
        "UPDATE facts SET created_at = ?, last_used_at = NULL WHERE id = ?",
        (iso, fact_id),
    )
    store._conn.commit()


async def _run(store, runner=None, **over):
    return await run_maintenance(
        store=store, settings=_settings(**over), runner=runner or FakeRunner()
    )


# --- the deterministic steps ---

async def test_decay_and_promotion_need_no_model(store):
    stale = store.add_fact("Was looking at flights to Lisbon")
    _backdate(store, stale)
    useful = store.add_fact("Prefers tea over coffee")
    for _ in range(3):
        store.touch_facts([useful])

    class _NoModel(FakeRunner):
        def stream(self, *a, **k):
            raise AssertionError("the deterministic steps must not call a model")

    # Consolidation and review will fail loudly; decay and promotion must not care.
    report = await _run(store, runner=_NoModel())

    assert report.decayed == 1
    assert report.promoted == 1
    assert store.get_fact(stale)["status"] == "forgotten"
    assert store.get_fact(useful)["tier"] == "durable"


async def test_pruning_ages_out_both_logs(store):
    store.log_exchange("old", "older")
    store._conn.execute("UPDATE conversations SET created_at = ?", (_LONG_AGO,))
    store.add_turn_events([{"kind": "turn", "created_at": _LONG_AGO}])
    store._conn.commit()

    report = await _run(store)

    assert report.pruned_messages == 2
    assert report.pruned_events == 1


# --- consolidation ---

async def test_consolidation_merges_duplicates(store):
    keep = store.add_fact("Has a dog named Mango")
    drop = store.add_fact("Owns a dog called Mango")

    runner = FakeRunner(
        f'{{"merge": [{{"keep": {keep}, "drop": [{drop}], '
        f'"content": "Has a dog named Mango"}}], "supersede": []}}'
    )
    report = await _run(store, runner=runner)

    assert report.merged == 1
    assert [f["content"] for f in store.all_facts()] == ["Has a dog named Mango"]


async def test_consolidation_supersedes_a_contradiction(store):
    old = store.add_fact("Lives in Boston")
    store.add_fact("Moved house recently")  # a second fact, so the step runs

    runner = FakeRunner(
        f'{{"merge": [], "supersede": [{{"old": {old}, "content": "Lives in Denver"}}]}}'
    )
    report = await _run(store, runner=runner)

    assert report.superseded == 1
    assert "Lives in Denver" in [f["content"] for f in store.all_facts()]
    assert store.get_fact(old)["status"] == "superseded"


async def test_consolidation_cannot_touch_a_fact_it_was_not_shown(store):
    """A model that invents an id must not reach a fact outside its batch."""
    store.add_fact("Has a dog named Mango")
    store.add_fact("Lives in Denver")
    hidden = store.add_fact("Hates cilantro")
    _backdate(store, hidden)  # outside the "changed recently" window

    runner = FakeRunner(f'{{"merge": [], "supersede": [{{"old": {hidden}, "content": "x"}}]}}')
    report = await _run(store, runner=runner)

    assert report.superseded == 0
    assert store.get_fact(hidden)["content"] == "Hates cilantro"


async def test_an_explicit_fact_is_never_dropped_for_a_guessed_one(store):
    """The user saying something outright outranks the extractor's inference."""
    guessed = store.add_fact("Might prefer tea")
    stated = store.add_fact("Prefers tea over coffee", tier="durable", source="explicit")

    runner = FakeRunner(
        f'{{"merge": [{{"keep": {guessed}, "drop": [{stated}]}}], "supersede": []}}'
    )
    report = await _run(store, runner=runner)

    assert report.merged == 0
    assert store.get_fact(stated)["status"] == "active"


async def test_consolidation_is_capped(store):
    keep = store.add_fact("keep this one")
    drops = [store.add_fact(f"duplicate {i}") for i in range(10)]

    runner = FakeRunner(
        f'{{"merge": [{{"keep": {keep}, "drop": {drops}}}], "supersede": []}}'
    )
    report = await _run(store, runner=runner, maintenance_max_changes=3)

    assert report.merged == 3
    assert store.fact_count() == 11 - 3


async def test_unparseable_consolidation_changes_nothing(store):
    store.add_fact("Has a dog named Mango")
    store.add_fact("Lives in Denver")

    report = await _run(store, runner=FakeRunner("I'm afraid I can't do that."))

    assert (report.merged, report.superseded) == (0, 0)
    assert store.fact_count() == 2


async def test_consolidation_skips_a_store_too_small_to_have_duplicates(store):
    store.add_fact("Has a dog named Mango")
    runner = FakeRunner()

    await _run(store, runner=runner)

    # One fact can't duplicate anything; don't spend a model call finding that out.
    assert all("Has a dog" not in str(c["payload"]) for c in runner.calls)


# --- self-review ---

async def test_self_review_writes_reflections_from_telemetry(store):
    store.add_turn_events([
        {"kind": "tool_call", "name": "web_search", "ok": False, "latency_ms": 50}
        for _ in range(4)
    ])

    # No facts in the store, so consolidation short-circuits and the review is the
    # only model call this pass makes.
    runner = FakeRunner('["Web search keeps failing; say so plainly."]')
    report = await _run(store, runner=runner)

    assert report.reflections == 1
    assert store.recent_reflections(5)[0]["note"] == (
        "Web search keeps failing; say so plainly."
    )


async def test_self_review_is_capped(store):
    store.add_turn_events([{"kind": "turn", "name": "voice"}])

    runner = FakeRunner('["one", "two", "three", "four", "five"]')
    report = await _run(store, runner=runner, maintenance_max_reflections=2)

    assert report.reflections == 2


async def test_no_telemetry_means_no_reflections(store):
    report = await _run(store)
    assert report.reflections == 0


# --- safety ---

async def test_one_failing_step_does_not_abort_the_others(store):
    stale = store.add_fact("Was looking at flights to Lisbon")
    _backdate(store, stale)
    # Two survivors, so consolidation still has something to look at after decay
    # forgets the stale one.
    store.add_fact("Has a dog named Mango")
    store.add_fact("Prefers tea over coffee")
    store.add_turn_events([{"kind": "turn"}])

    class _Boom(FakeRunner):
        def stream(self, *a, **k):
            raise RuntimeError("model unavailable")

    report = await _run(store, runner=_Boom())

    # Both LLM steps failed and were recorded...
    assert len(report.errors) == 2
    # ...but the deterministic work still landed.
    assert report.decayed == 1
    assert store.get_fact(stale)["status"] == "forgotten"


async def test_the_pass_never_raises(store):
    class _Boom(FakeRunner):
        def stream(self, *a, **k):
            raise RuntimeError("everything is broken")

    report = await _run(store, runner=_Boom())
    assert isinstance(report, MaintenanceReport)


async def test_running_twice_changes_nothing_the_second_time(store):
    stale = store.add_fact("Was looking at flights to Lisbon")
    _backdate(store, stale)
    useful = store.add_fact("Prefers tea over coffee")
    for _ in range(3):
        store.touch_facts([useful])

    first = await _run(store)
    second = await _run(store)

    assert (first.decayed, first.promoted) == (1, 1)
    assert (second.decayed, second.promoted, second.merged, second.superseded) == (
        0, 0, 0, 0,
    )


async def test_the_pass_is_a_no_op_when_memory_is_off(store):
    store.add_fact("Was looking at flights to Lisbon")

    report = await run_maintenance(
        store=store, settings=_settings(feature_memory=False), runner=FakeRunner()
    )

    assert report.summary().startswith("decayed=0 promoted=0")
    assert store.fact_count() == 1


async def test_durable_facts_survive_the_pass(store):
    """They leave only by being superseded or explicitly forgotten — never by age."""
    durable = store.add_fact("Lives in Denver", tier="durable")
    _backdate(store, durable)

    await _run(store)

    assert store.get_fact(durable)["status"] == "active"


# --- the loop ---

async def test_the_loop_does_nothing_before_its_startup_delay(store):
    """Not cosmetic: the test suite starts the app repeatedly, and an immediate pass
    would run against the real database every time."""
    ran = []

    task = asyncio.create_task(
        maintenance_loop(_settings(maintenance_startup_delay_s=60.0))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ran == []


async def test_the_loop_cancels_cleanly(store):
    task = asyncio.create_task(
        maintenance_loop(_settings(maintenance_startup_delay_s=0.01,
                                   maintenance_interval_s=60.0))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()
