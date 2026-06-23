"""Tests for the context builder — ranking and the compressed prompt block."""

import pytest

from app.config import Settings
from app.memory.context import _rank_facts, build_context, build_memory_view
from app.memory.store import MemoryStore


def _facts(*contents):
    """Build fact rows newest-first, like the store returns them."""
    return [{"content": c} for c in contents]


def _settings(**over):
    base = dict(feature_memory=True, memory_max_facts=12, recent_recap_messages=6)
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


# --- _rank_facts ---

def test_rank_no_query_returns_newest_up_to_limit():
    facts = _facts("c", "b", "a")
    assert [f["content"] for f in _rank_facts(facts, None, 2)] == ["c", "b"]


def test_rank_prefers_keyword_overlap_with_query():
    facts = _facts("Likes hiking in the mountains", "Has a dog named Mango")
    ranked = _rank_facts(facts, "tell me about the dog", 2)
    assert ranked[0]["content"] == "Has a dog named Mango"


def test_rank_falls_back_to_recent_when_nothing_overlaps():
    facts = _facts("Likes tea", "Plays guitar")
    ranked = _rank_facts(facts, "quantum chromodynamics", 1)
    # No overlap -> most recent fact rather than nothing.
    assert ranked == [{"content": "Likes tea"}]


def test_rank_limit_zero_is_empty():
    assert _rank_facts(_facts("a"), None, 0) == []


# --- build_context ---

async def test_build_context_formats_facts_and_tasks(store):
    store.add_fact("Likes hiking")
    store.add_task("buy a tent")

    block = await build_context("planning a hike", store=store, settings=_settings())

    assert block is not None
    assert "Likes hiking" in block
    assert "buy a tent" in block
    # Both sections are present.
    assert "remember about your user" in block
    assert "Open tasks" in block


async def test_build_context_returns_none_when_empty(store):
    block = await build_context("anything", store=store, settings=_settings())
    assert block is None


async def test_build_context_returns_none_when_memory_disabled(store):
    store.add_fact("Likes hiking")
    block = await build_context(
        "hike", store=store, settings=_settings(feature_memory=False)
    )
    assert block is None


async def test_build_context_caps_facts(store):
    for i in range(10):
        store.add_fact(f"fact number {i}")

    block = await build_context(
        None, store=store, settings=_settings(memory_max_facts=3)
    )
    # Only the cap's worth of fact bullets appear.
    fact_lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(fact_lines) == 3


# --- recent-conversation recap (cold-start continuity) ---

async def test_recap_replays_recent_messages_when_requested(store):
    store.log_exchange("Remind me to call mom", "Sure, I'll remind you.")

    block, items = await build_memory_view(
        "anything", include_recap=True, store=store, settings=_settings()
    )

    assert block is not None
    assert "Picking up from your last conversation" in block
    assert "They: Remind me to call mom" in block
    assert "You: Sure, I'll remind you." in block
    # The recap is prompt-only — it never leaks into the client memory items.
    assert items == []


async def test_recap_omitted_by_default(store):
    store.log_exchange("hello", "hi there")
    block, _ = await build_memory_view("anything", store=store, settings=_settings())
    # No include_recap -> no replay (the live history already covers recent context).
    assert block is None


async def test_recap_disabled_by_zero_cap(store):
    store.log_exchange("hello", "hi there")
    block, _ = await build_memory_view(
        "anything",
        include_recap=True,
        store=store,
        settings=_settings(recent_recap_messages=0),
    )
    assert block is None


async def test_recap_appends_after_facts(store):
    store.add_fact("Likes hiking")
    store.log_exchange("what's the weather", "Looks clear today.")

    block, _ = await build_memory_view(
        "hiking", include_recap=True, store=store, settings=_settings()
    )
    # Facts come first, the recap after.
    assert block.index("Likes hiking") < block.index("Picking up from")
