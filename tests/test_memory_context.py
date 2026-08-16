"""Tests for the context builder — ranking and the compressed prompt block.

Ranking now runs against a real store rather than a hand-built list, because the
candidate pool comes from the store's index. That's the point of the rewrite: the
old ranker read every fact into Python on every turn, so it could be tested on a
list; the new one asks the index a bounded question.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.memory.context import _score, build_memory_view, rank_facts
from app.memory.store import MemoryStore


def _settings(**over):
    base = dict(
        feature_memory=True,
        memory_max_facts=12,
        memory_max_chars=1200,
        memory_candidates=40,
        memory_always_durable=5,
        memory_max_tasks=8,
        recent_recap_messages=6,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


async def _rank(store, query, **over):
    return await rank_facts(query, store=store, settings=_settings(**over))


# --- ranking ---

async def test_rank_prefers_facts_relevant_to_the_question(store):
    store.add_fact("Likes hiking in the mountains")
    store.add_fact("Has a dog named Mango")

    ranked = await _rank(store, "tell me about the dog")
    assert ranked[0]["content"] == "Has a dog named Mango"


async def test_rank_falls_back_to_recent_when_nothing_matches(store):
    store.add_fact("Plays guitar")
    store.add_fact("Likes tea")

    ranked = await _rank(store, "quantum chromodynamics")
    # No match and nothing durable -> the newest facts, so a young memory still has
    # something to say rather than a blank slate.
    assert [f["content"] for f in ranked] == ["Likes tea", "Plays guitar"]


async def test_durable_facts_are_included_even_when_irrelevant(store):
    """The single biggest miss in pure relevance ranking.

    Identity-level knowledge rarely shares words with the question — "what should I
    make for dinner" has nothing in common with "Lives in Denver" — so a relevance-
    only ranker drops precisely the facts that matter most.
    """
    store.add_fact("Lives in Denver", tier="durable")
    store.add_fact("Has a dog named Mango")

    ranked = await _rank(store, "tell me about the dog")
    contents = [f["content"] for f in ranked]
    assert "Has a dog named Mango" in contents
    assert "Lives in Denver" in contents


async def test_search_matches_across_word_endings(store):
    """The index stems, so asking about "dogs" finds a fact about a "dog"."""
    store.add_fact("Has a dog named Mango")
    ranked = await _rank(store, "dogs")
    assert [f["content"] for f in ranked] == ["Has a dog named Mango"]


async def test_common_words_do_not_make_everything_relevant(store):
    """"tell me about the dog" must not match a fact merely for containing "the"."""
    store.add_fact("Likes hiking in the mountains")
    store.add_fact("Has a dog named Mango")

    ranked = await _rank(store, "tell me about the dog")
    assert [f["content"] for f in ranked] == ["Has a dog named Mango"]


# --- how the signals trade off ---
#
# These score facts directly rather than going through a search, because the
# question is how the scorer weighs tier/usage/recency *given* equal relevance —
# routing that through bm25 would really be testing bm25's document-length
# normalization instead.

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _row(**over):
    base = dict(
        id=1,
        content="a fact",
        tier="short",
        confidence=0.6,
        use_count=0,
        last_used_at=_NOW.isoformat(),
        created_at=_NOW.isoformat(),
    )
    base.update(over)
    return base


def test_a_durable_fact_outranks_a_provisional_one():
    durable = _score(_row(tier="durable"), 1.0, _NOW)
    provisional = _score(_row(tier="short"), 1.0, _NOW)
    assert durable > provisional


def test_a_repeatedly_useful_fact_outranks_an_unused_one():
    useful = _score(_row(use_count=5), 1.0, _NOW)
    unused = _score(_row(use_count=0), 1.0, _NOW)
    assert useful > unused


def test_a_long_untouched_fact_ranks_below_a_fresh_one():
    stale = _row(last_used_at=(_NOW - timedelta(days=120)).isoformat())
    assert _score(stale, 1.0, _NOW) < _score(_row(), 1.0, _NOW)


def test_relevance_outweighs_every_other_signal_combined():
    """A fact that actually answers the question beats a well-established one that
    doesn't — otherwise durable facts would crowd out the answer."""
    matched = _score(_row(tier="short", confidence=0.5, use_count=0), 1.0, _NOW)
    unmatched = _score(_row(tier="durable", confidence=1.0, use_count=50), 0.0, _NOW)
    assert matched > unmatched


def test_a_missing_timestamp_does_not_break_scoring():
    assert _score(_row(last_used_at=None, created_at=None), 1.0, _NOW) > 0
    assert _score(_row(last_used_at="not a date", created_at=None), 1.0, _NOW) > 0


async def test_ranking_excludes_forgotten_and_superseded_facts(store):
    gone = store.add_fact("Lives in Boston")
    store.supersede_fact(gone, "Lives in Denver")
    dropped = store.add_fact("Hates cilantro")
    store.forget_fact(dropped)

    ranked = await _rank(store, "lives cilantro Denver")
    assert [f["content"] for f in ranked] == ["Lives in Denver"]


async def test_rank_respects_the_fact_cap(store):
    for i in range(10):
        store.add_fact(f"fact number {i}")

    assert len(await _rank(store, "fact", memory_max_facts=3)) == 3
    assert await _rank(store, "fact", memory_max_facts=0) == []


async def test_rank_respects_the_character_budget(store):
    """Twelve long facts cost far more than twelve short ones, and the row cap alone
    can't see the difference — the block is paid for on every single LLM call."""
    for i in range(6):
        store.add_fact(f"fact number {i} " + "padding " * 20)

    ranked = await _rank(store, "fact", memory_max_chars=200)
    assert 0 < len(ranked) < 6
    assert sum(len(f["content"]) for f in ranked) <= 200


async def test_a_single_oversized_fact_is_still_returned(store):
    """The budget stops the block growing; it never returns nothing at all."""
    store.add_fact("fact " + "padding " * 100)
    assert len(await _rank(store, "fact", memory_max_chars=50)) == 1


# --- the prompt block ---

async def test_build_view_formats_facts_and_tasks(store):
    store.add_fact("Likes hiking")
    store.add_task("buy a tent")

    view = await build_memory_view("planning a hike", store=store, settings=_settings())

    assert "Likes hiking" in view.block
    assert "buy a tent" in view.block
    assert "remember about your user" in view.block
    assert "Open tasks" in view.block


async def test_the_block_carries_ids_so_facts_can_be_acted_on(store):
    fact_id = store.add_fact("Lives in Boston")

    view = await build_memory_view("where do I live", store=store, settings=_settings())

    # Without ids, correcting a fact costs a search round trip first.
    assert f"[#{fact_id}]" in view.block
    assert view.fact_ids == [fact_id]

    (fact,) = view.facts
    # The three a Phase-3 client already reads, unchanged.
    assert fact["id"] == fact_id
    assert fact["content"] == "Lives in Boston"
    assert fact["tier"] == "short"
    # Plus what a memory panel needs to be more than a bulleted list. These cost
    # nothing — `rank_facts` already returns the whole row and this used to throw
    # them away — and they are what lets a fact show whether it has earned its keep.
    assert fact["confidence"] == pytest.approx(0.6)
    assert fact["use_count"] == 0
    assert fact["source"] == "extracted"
    assert "last_used_at" in fact
    assert "category" in fact
    # Never the audit columns: this view is of active facts, so `status` is a
    # constant here and `superseded_by` only means anything in a history view.
    assert "status" not in fact
    assert "superseded_by" not in fact


async def test_open_tasks_are_capped(store):
    for i in range(12):
        store.add_task(f"task {i}")

    view = await build_memory_view(None, store=store, settings=_settings(memory_max_tasks=3))
    task_lines = [ln for ln in view.block.splitlines() if ln.startswith("- [#")]
    assert len(task_lines) == 3


async def test_build_view_is_empty_when_nothing_is_known(store):
    view = await build_memory_view("anything", store=store, settings=_settings())
    assert view.block is None
    assert view.items == []
    assert view.fact_ids == []


async def test_build_view_is_empty_when_memory_disabled(store):
    store.add_fact("Likes hiking")
    view = await build_memory_view(
        "hike", store=store, settings=_settings(feature_memory=False)
    )
    assert view.block is None
    assert view.items == []


# --- recent-conversation recap (cold-start continuity) ---

async def test_recap_replays_recent_messages_when_requested(store):
    store.log_exchange("Remind me to call mom", "Sure, I'll remind you.")

    view = await build_memory_view(
        "anything", include_recap=True, store=store, settings=_settings()
    )

    assert "Picking up from your last conversation" in view.block
    assert "They: Remind me to call mom" in view.block
    assert "You: Sure, I'll remind you." in view.block
    # The recap is prompt-only — it never leaks into the client memory items.
    assert view.items == []


async def test_recap_omitted_by_default(store):
    store.log_exchange("hello", "hi there")
    view = await build_memory_view("anything", store=store, settings=_settings())
    # No include_recap -> no replay (the live history already covers recent context).
    assert view.block is None


async def test_recap_disabled_by_zero_cap(store):
    store.log_exchange("hello", "hi there")
    view = await build_memory_view(
        "anything",
        include_recap=True,
        store=store,
        settings=_settings(recent_recap_messages=0),
    )
    assert view.block is None


async def test_recap_appends_after_facts(store):
    store.add_fact("Likes hiking")
    store.log_exchange("what's the weather", "Looks clear today.")

    view = await build_memory_view(
        "hiking", include_recap=True, store=store, settings=_settings()
    )
    # Facts come first, the recap after.
    assert view.block.index("Likes hiking") < view.block.index("Picking up from")
