"""Tests for the memory tools — how Amber curates what she remembers.

These are the tools that close the gap where memory happened *to* Amber: she could
neither search, commit, correct, nor drop a fact, so a user saying "no, I moved" left
the wrong fact in every future prompt.
"""

import pytest

import app.config as config_module
import app.tools.memory_tools as memory_tools
from app.memory.store import MemoryStore
from app.tools import get_tool_schemas
from app.tools.memory_tools import (
    complete_reminder,
    correct_fact,
    forget_fact,
    list_reminders,
    remember_fact,
    search_memory,
)


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore(":memory:")
    monkeypatch.setattr(memory_tools, "get_store", lambda: s)
    yield s
    s.close()


# --- search_memory ---

async def test_search_memory_returns_facts_with_ids(store):
    fact_id = store.add_fact("Has a dog named Mango")
    store.add_fact("Prefers tea over coffee")

    out = await search_memory("dog")

    # The id is the point: it's what correct_fact and forget_fact need.
    assert f"#{fact_id}: Has a dog named Mango" in out
    assert "tea" not in out


async def test_search_memory_reports_a_miss_without_inviting_a_guess(store):
    out = await search_memory("anything")
    assert "Nothing in memory matches" in out
    assert "rather than guessing" in out


async def test_search_memory_needs_a_query(store):
    assert "Error" in await search_memory("   ")


async def test_search_memory_caps_the_limit(store):
    for i in range(40):
        store.add_fact(f"fact about dogs number {i}")

    out = await search_memory("dogs", limit=999)
    assert len(out.splitlines()) <= 21  # header + at most 20 facts


# --- remember_fact ---

async def test_remember_fact_saves_durably_and_reports_the_id(store):
    out = await remember_fact("Has a dog named Mango", category="relationship")

    fact = store.all_facts()[0]
    assert out == f"Saved #{fact['id']}: Has a dog named Mango"
    # An explicit "remember this" is the strongest evidence there is, so it skips
    # the probation the automatic writer's facts serve.
    assert fact["tier"] == "durable"
    assert fact["source"] == "explicit"
    assert fact["confidence"] == pytest.approx(0.9)
    assert fact["category"] == "relationship"


async def test_remember_fact_says_so_when_it_already_knew(store):
    """Otherwise Amber says "got it" for something that changed nothing."""
    store.add_fact("Prefers tea over coffee")

    out = await remember_fact("prefers TEA over coffee")

    assert "Already knew that" in out
    assert store.fact_count() == 1


async def test_remember_fact_promotes_a_fact_the_writer_had_only_guessed(store):
    fact_id = store.add_fact("Is learning Spanish")  # extracted -> short tier
    await remember_fact("Is learning Spanish")
    assert store.get_fact(fact_id)["tier"] == "durable"


async def test_remember_fact_needs_content(store):
    assert "Error" in await remember_fact("  ")
    assert store.fact_count() == 0


# --- correct_fact ---

async def test_correct_fact_supersedes_and_keeps_the_old_one_on_record(store):
    old = store.add_fact("Lives in Boston")

    out = await correct_fact(old, "Lives in Denver")

    new = store.all_facts()[0]
    assert out == f"Replaced #{old} ('Lives in Boston') with #{new['id']}: Lives in Denver"
    assert [f["content"] for f in store.all_facts()] == ["Lives in Denver"]
    assert store.get_fact(old)["status"] == "superseded"
    assert store.get_fact(old)["superseded_by"] == new["id"]


async def test_correct_fact_points_at_the_recovery_path_for_a_bad_id(store):
    out = await correct_fact(9999, "Lives in Denver")
    assert "no active fact #9999" in out
    assert "search_memory" in out


async def test_correct_fact_will_not_correct_an_already_forgotten_fact(store):
    fact_id = store.add_fact("Lives in Boston")
    store.forget_fact(fact_id)
    assert "no active fact" in await correct_fact(fact_id, "Lives in Denver")


async def test_correct_fact_needs_the_correction(store):
    fact_id = store.add_fact("Lives in Boston")
    assert "Error" in await correct_fact(fact_id, "   ")
    assert store.get_fact(fact_id)["status"] == "active"


# --- forget_fact ---

async def test_forget_fact_echoes_what_it_dropped(store):
    fact_id = store.add_fact("Hates cilantro")

    out = await forget_fact(fact_id)

    # Echoed so Amber can confirm out loud what she just dropped.
    assert out == f"Forgotten #{fact_id}: Hates cilantro"
    assert store.all_facts() == []


async def test_forget_fact_reports_an_unknown_id(store):
    out = await forget_fact(9999)
    assert "no active fact #9999" in out
    assert "search_memory" in out


# --- reminders ---

async def test_reminders_can_be_listed_and_cleared(store):
    rid = store.add_reminder("call mom", "2026-06-22T17:30:00")

    listed = await list_reminders()
    assert f"#{rid}: call mom" in listed
    # Read back in words and in the user's own zone — the model speaks this aloud, and
    # the column holds UTC now that reminders actually fire.
    assert "June 22" in listed
    assert "5:30 PM" in listed

    assert await complete_reminder(rid) == f"Cleared reminder #{rid}."
    assert "no pending reminders" in (await list_reminders()).lower()


async def test_completing_an_unknown_reminder_points_at_the_list(store):
    out = await complete_reminder(9999)
    assert "no pending reminder #9999" in out
    assert "list_reminders" in out


# --- wiring ---

def test_query_tools_are_marked_read_only():
    """The ecosystem convention: a caller can tell what's safe to retry."""
    schemas = {s["name"]: s for s in get_tool_schemas()}

    assert schemas["search_memory"].get("x_agent") == {"read_only": True}
    assert schemas["list_reminders"].get("x_agent") == {"read_only": True}
    # Mutating tools carry no such marker.
    assert "x_agent" not in schemas["forget_fact"]
    assert "x_agent" not in schemas["remember_fact"]


def test_memory_tools_are_hidden_when_memory_is_off(monkeypatch):
    monkeypatch.setenv("AMBER_FEATURE_MEMORY", "false")
    config_module.get_settings.cache_clear()
    try:
        names = {s["name"] for s in get_tool_schemas()}
        assert "remember_fact" not in names
        assert "search_memory" not in names
        assert "web_search" in names  # unrelated tools are unaffected
    finally:
        monkeypatch.delenv("AMBER_FEATURE_MEMORY", raising=False)
        config_module.get_settings.cache_clear()


def test_memory_tools_reach_the_model():
    names = {s["name"] for s in get_tool_schemas()}
    assert {
        "search_memory",
        "remember_fact",
        "correct_fact",
        "forget_fact",
        "list_reminders",
        "complete_reminder",
    } <= names
