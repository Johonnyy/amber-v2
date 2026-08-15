"""Tests for the SQLite memory store (no network, in-memory DB)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.memory.store import MemoryStore


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def test_add_and_list_facts_newest_first(store):
    store.add_fact("Likes hiking")
    store.add_fact("Has a dog named Mango")

    facts = store.all_facts()
    assert [f["content"] for f in facts] == ["Has a dog named Mango", "Likes hiking"]
    assert store.fact_count() == 2


def test_duplicate_facts_reinforce_rather_than_duplicate(store):
    """Saying the same thing twice strengthens one row instead of making two.

    A repeat mention is the clearest evidence a fact matters, so it raises the use
    count and confidence of the fact already on record. `fact_exists` is how a
    caller tells "new" from "reinforced".
    """
    first = store.add_fact("Prefers tea over coffee")
    dup = store.add_fact("prefers TEA over coffee")  # same fact, different case

    assert first is not None
    assert dup == first  # same row, not a second one
    assert store.fact_count() == 1

    fact = store.get_fact(first)
    assert fact["use_count"] == 1
    assert fact["confidence"] > 0.6


def test_an_explicit_repeat_can_promote_a_fact_but_never_demote_it(store):
    fact_id = store.add_fact("Has a dog named Mango")  # extracted -> short
    assert store.get_fact(fact_id)["tier"] == "short"

    store.add_fact("Has a dog named Mango", tier="durable")
    assert store.get_fact(fact_id)["tier"] == "durable"

    # A later background extraction must not push it back down.
    store.add_fact("Has a dog named Mango", tier="short")
    assert store.get_fact(fact_id)["tier"] == "durable"


def test_fact_exists_only_sees_active_facts(store):
    fact_id = store.add_fact("Lives in Boston")
    assert store.fact_exists("lives in BOSTON") is True

    store.forget_fact(fact_id)
    assert store.fact_exists("Lives in Boston") is False


def test_forgetting_a_fact_hides_it_but_keeps_it_recoverable(store):
    fact_id = store.add_fact("Likes cilantro")

    forgotten = store.forget_fact(fact_id)
    assert forgotten["content"] == "Likes cilantro"  # echoed back for confirmation
    assert store.all_facts() == []
    assert store.get_fact(fact_id)["status"] == "forgotten"

    # Forgetting an already-forgotten fact reports no change.
    assert store.forget_fact(fact_id) is None
    # ...and the same fact can be learned again later.
    assert store.add_fact("Likes cilantro") != fact_id


def test_superseding_links_the_old_fact_to_its_replacement(store):
    old = store.add_fact("Lives in Boston")
    new = store.supersede_fact(old, "Lives in Denver")

    assert [f["content"] for f in store.all_facts()] == ["Lives in Denver"]
    assert store.get_fact(old)["status"] == "superseded"
    assert store.get_fact(old)["superseded_by"] == new
    assert store.supersede_fact(9999, "nope") is None


def test_search_finds_facts_by_word(store):
    store.add_fact("Has a dog named Mango")
    store.add_fact("Prefers tea over coffee")

    assert [f["content"] for f in store.search_facts("dog")] == ["Has a dog named Mango"]
    # An empty or wordless query returns nothing rather than everything.
    assert store.search_facts("") == []
    assert store.search_facts("a") == []


def test_search_excludes_forgotten_facts(store):
    fact_id = store.add_fact("Has a dog named Mango")
    store.forget_fact(fact_id)
    assert store.search_facts("dog") == []


def test_touch_facts_records_use(store):
    a = store.add_fact("Likes hiking")
    b = store.add_fact("Has a dog")

    store.touch_facts([a, b, 9999])  # unknown ids are harmless

    assert store.get_fact(a)["use_count"] == 1
    assert store.get_fact(a)["last_used_at"] is not None
    store.touch_facts([])  # no-op, no crash
    assert store.get_fact(b)["use_count"] == 1


def test_blank_fact_is_rejected(store):
    assert store.add_fact("   ") is None
    assert store.fact_count() == 0


def test_recent_facts_respects_limit(store):
    for i in range(5):
        store.add_fact(f"fact {i}")

    recent = store.recent_facts(2)
    assert [f["content"] for f in recent] == ["fact 4", "fact 3"]
    assert store.recent_facts(0) == []


def test_log_exchange_records_both_messages_in_order(store):
    store.log_exchange("what's the weather", "It's sunny.")

    msgs = store.recent_messages(10)
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "what's the weather"),
        ("assistant", "It's sunny."),
    ]


def test_log_exchange_skips_empty_sides(store):
    store.log_exchange("hi", "")
    msgs = store.recent_messages(10)
    assert [m["role"] for m in msgs] == ["user"]


def test_tasks_open_complete_lifecycle(store):
    a = store.add_task("buy milk")
    store.add_task("call dentist")

    open_now = store.open_tasks()
    assert [t["description"] for t in open_now] == ["buy milk", "call dentist"]

    assert store.complete_task(a) is True
    assert [t["description"] for t in store.open_tasks()] == ["call dentist"]

    # Completing an already-done (or unknown) task reports no change.
    assert store.complete_task(a) is False
    assert store.complete_task(9999) is False


# --- fact lifecycle (what the maintenance pass drives) ---

def _backdate(store, fact_id, iso):
    """Age a fact by rewriting its timestamps, so decay windows are testable."""
    store._conn.execute(
        "UPDATE facts SET created_at = ?, last_used_at = NULL WHERE id = ?",
        (iso, fact_id),
    )
    store._conn.commit()


_LONG_AGO = "2020-01-01T00:00:00+00:00"


def test_promote_lifts_facts_that_keep_proving_useful(store):
    useful = store.add_fact("Prefers tea over coffee")
    ignored = store.add_fact("Mentioned a film once")
    for _ in range(3):  # three separate turns drew on it
        store.touch_facts([useful])

    assert store.promote_facts(min_uses=3) == 1

    assert store.get_fact(useful)["tier"] == "durable"
    assert store.get_fact(ignored)["tier"] == "short"
    # Idempotent: nothing left to promote.
    assert store.promote_facts(min_uses=3) == 0


def test_decay_forgets_stale_unused_short_facts(store):
    stale = store.add_fact("Was looking at flights to Lisbon")
    _backdate(store, stale, _LONG_AGO)

    assert store.decay_facts(short_ttl_days=30, session_ttl_hours=12) == 1
    assert store.get_fact(stale)["status"] == "forgotten"


def test_decay_spares_a_stale_fact_that_has_been_used(store):
    """Age alone isn't evidence of irrelevance — use is evidence of the opposite."""
    used = store.add_fact("Has a dog named Mango")
    for _ in range(2):
        store.touch_facts([used])
    _backdate(store, used, _LONG_AGO)
    store._conn.execute(
        "UPDATE facts SET last_used_at = ? WHERE id = ?", (_LONG_AGO, used)
    )
    store._conn.commit()

    assert store.decay_facts(short_ttl_days=30, session_ttl_hours=12) == 0
    assert store.get_fact(used)["status"] == "active"


def test_decay_never_touches_durable_facts(store):
    durable = store.add_fact("Lives in Denver", tier="durable")
    _backdate(store, durable, _LONG_AGO)

    assert store.decay_facts(short_ttl_days=30, session_ttl_hours=12) == 0
    assert store.get_fact(durable)["status"] == "active"


def test_decay_takes_session_facts_much_sooner(store):
    fleeting = store.add_fact("Is on a call right now", tier="session")
    recent_short = store.add_fact("Is reading a book about Rome")

    # A day old: past the session window, nowhere near the short one.
    day_old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    _backdate(store, fleeting, day_old)
    _backdate(store, recent_short, day_old)

    assert store.decay_facts(short_ttl_days=30, session_ttl_hours=12) == 1
    assert store.get_fact(fleeting)["status"] == "forgotten"
    assert store.get_fact(recent_short)["status"] == "active"


def test_decay_is_disabled_by_a_zero_ttl(store):
    fact_id = store.add_fact("Was looking at flights to Lisbon")
    _backdate(store, fact_id, _LONG_AGO)

    assert store.decay_facts(short_ttl_days=0, session_ttl_hours=0) == 0
    assert store.get_fact(fact_id)["status"] == "active"


def test_facts_changed_since_finds_recent_work(store):
    store.add_fact("Likes hiking")
    assert len(store.facts_changed_since("2020-01-01T00:00:00+00:00", 10)) == 1
    assert store.facts_changed_since("2099-01-01T00:00:00+00:00", 10) == []


# --- conversation retention ---

def test_prune_conversations_drops_only_old_messages(store):
    store.log_exchange("old question", "old answer")
    store._conn.execute("UPDATE conversations SET created_at = ?", (_LONG_AGO,))
    store._conn.commit()
    store.log_exchange("new question", "new answer")

    assert store.prune_conversations(keep_days=30) == 2
    assert [m["content"] for m in store.recent_messages(10)] == [
        "new question",
        "new answer",
    ]
    assert store.prune_conversations(keep_days=0) == 0  # disabled


def test_reminders_persist_pending(store):
    store.add_reminder("call mom", "2026-06-22T17:30:00")
    store.add_reminder("water plants")  # no time

    pending = store.pending_reminders()
    assert [(r["text"], r["remind_at"]) for r in pending] == [
        ("call mom", "2026-06-22T17:30:00"),
        ("water plants", None),
    ]
    assert all(r["status"] == "pending" for r in pending)


def test_reminders_can_be_completed(store):
    """They were write-only before: nothing could ever mark one done."""
    rid = store.add_reminder("call mom", "2026-06-22T17:30:00")

    assert store.complete_reminder(rid) is True
    assert store.pending_reminders() == []
    # Completing it again, or an unknown one, reports no change.
    assert store.complete_reminder(rid) is False
    assert store.complete_reminder(9999) is False


def test_due_reminders_only_returns_ones_whose_time_has_come(store):
    store.add_reminder("call mom", "2026-06-22T17:30:00")
    store.add_reminder("call dentist", "2099-01-01T00:00:00")
    store.add_reminder("water plants")  # no time -> never "due"

    due = store.due_reminders("2026-06-22T18:00:00")
    assert [r["text"] for r in due] == ["call mom"]


# --- telemetry & reflections ---

def test_turn_events_batch_insert_and_summarize(store):
    assert store.add_turn_events([
        {"kind": "tool_call", "name": "web_search", "ok": True, "latency_ms": 900},
        {"kind": "tool_call", "name": "web_search", "ok": True, "latency_ms": 1100},
        {"kind": "tool_call", "name": "web_search", "ok": False, "latency_ms": 50},
    ]) == 3
    assert store.add_turn_events([]) == 0

    summary = {(r["name"], r["ok"]): r for r in store.event_summary("2020-01-01T00:00:00+00:00")}
    assert summary[("web_search", 1)]["count"] == 2
    assert summary[("web_search", 1)]["avg_latency_ms"] == 1000
    assert summary[("web_search", 0)]["count"] == 1


def test_prune_turn_events_drops_old_rows(store):
    store.add_turn_events([{"kind": "turn", "created_at": _LONG_AGO}])
    store.add_turn_events([{"kind": "turn"}])

    assert store.prune_turn_events(keep_days=30) == 1
    assert len(store.event_summary("2020-01-01T00:00:00+00:00")) == 1


def test_reflections_round_trip_newest_first(store):
    store.add_reflection("Searches often fail on sports scores.")
    store.add_reflection("They usually mean this week when they say 'soon'.")
    assert store.add_reflection("   ") is None

    notes = [r["note"] for r in store.recent_reflections(5)]
    assert notes == [
        "They usually mean this week when they say 'soon'.",
        "Searches often fail on sports scores.",
    ]
    assert store.last_reflection_at() is not None
