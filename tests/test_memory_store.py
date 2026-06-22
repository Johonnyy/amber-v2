"""Tests for the SQLite memory store (no network, in-memory DB)."""

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


def test_duplicate_facts_are_ignored_case_insensitively(store):
    first = store.add_fact("Prefers tea over coffee")
    dup = store.add_fact("prefers TEA over coffee")  # same fact, different case

    assert first is not None
    assert dup is None  # collision -> not stored
    assert store.fact_count() == 1


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
