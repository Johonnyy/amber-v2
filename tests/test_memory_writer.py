"""Tests for the memory writer — fact parsing, extraction, and persistence.

The LLM is always faked: either a fake `agent_runtime` runner (for `extract_facts`)
or a monkeypatched `extract_facts` (for `remember`). No network.
"""

import pytest

import app.memory.writer as writer
from app.config import Settings
from app.memory.store import MemoryStore
from app.memory.writer import _parse_facts, extract_facts, remember


# --- fake agent_runtime runner ---
#
# The writer streams and joins rather than calling `run()`, because `run()` drives
# the sentence splitter — right for speech, wrong for a JSON array. The fake
# therefore yields the response in fragments, which is also how a real model
# streams one.

class FakeRunner:
    def __init__(self, text, *, model=None):
        self._text = text
        self.model = model
        self.calls = []

    def stream(self, messages, *, system=None, conversation_id=None, depth=0):
        self.calls.append(
            {"messages": messages, "system": system, "conversation_id": conversation_id}
        )

        async def gen():
            # Split mid-string so a writer that assumed one whole chunk would fail.
            half = len(self._text) // 2
            yield self._text[:half]
            yield self._text[half:]

        return gen()


def _settings(**over):
    base = dict(feature_memory=True, memory_max_new_facts=5)
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


# --- _parse_facts ---
#
# The extractor now returns records ({content, category, tier, replaces}), but a
# cheap model reverts to a bare string array often enough that both shapes have to
# parse. `_contents` keeps the assertions about the part that matters.

def _contents(records):
    return [r["content"] for r in records]


def test_parse_facts_plain_json_array():
    assert _contents(_parse_facts('["Likes tea", "Has a cat"]', 5)) == [
        "Likes tea",
        "Has a cat",
    ]


def test_parse_facts_object_form_carries_category_and_tier():
    raw = '[{"fact": "Has a dog named Mango", "category": "relationship", "durable": true}]'
    facts = _parse_facts(raw, 5)

    assert facts == [
        {
            "content": "Has a dog named Mango",
            "category": "relationship",
            "tier": "durable",
            "replaces": None,
        }
    ]


def test_parse_facts_non_durable_object_lands_in_the_short_tier():
    raw = '[{"fact": "Is redecorating the kitchen", "durable": false}]'
    assert _parse_facts(raw, 5)[0]["tier"] == "short"


def test_parse_facts_bare_string_defaults_to_short_and_no_category():
    assert _parse_facts('["Likes tea"]', 5) == [
        {"content": "Likes tea", "category": None, "tier": "short", "replaces": None}
    ]


def test_parse_facts_keeps_a_replaces_pointer():
    raw = '[{"fact": "Lives in Denver", "replaces": "Lives in Boston"}]'
    assert _parse_facts(raw, 5)[0]["replaces"] == "Lives in Boston"


def test_parse_facts_rejects_an_unknown_category():
    raw = '[{"fact": "Likes tea", "category": "beverages"}]'
    assert _parse_facts(raw, 5)[0]["category"] is None


def test_parse_facts_strips_code_fence():
    raw = '```json\n["Lives in Berlin"]\n```'
    assert _contents(_parse_facts(raw, 5)) == ["Lives in Berlin"]


def test_parse_facts_falls_back_to_lines():
    raw = "- Likes tea\n- Has a cat"
    assert _contents(_parse_facts(raw, 5)) == ["Likes tea", "Has a cat"]


def test_parse_facts_unwraps_a_facts_object():
    raw = '{"facts": [{"fact": "Likes tea"}]}'
    assert _contents(_parse_facts(raw, 5)) == ["Likes tea"]


def test_parse_facts_empty_array():
    assert _parse_facts("[]", 5) == []


def test_parse_facts_drops_none_sentinels():
    assert _parse_facts('["none"]', 5) == []


def test_parse_facts_respects_limit():
    raw = '["a", "b", "c", "d"]'
    assert _contents(_parse_facts(raw, 2)) == ["a", "b"]


# --- structural debris ---
#
# A preamble before the fence defeats both `json.loads` and the fence strip, so
# the reply used to reach the line reader, which stored "```json" and "[]" as
# facts. They then looked undeletable in the panel: forgetting one only hides the
# row, and `add_fact` matches active rows only, so the next junk extraction
# inserted a fresh one saying the same thing.


def test_parse_facts_finds_the_array_after_a_preamble():
    raw = 'Here is the JSON:\n```json\n["Lives in Berlin"]\n```'
    assert _contents(_parse_facts(raw, 5)) == ["Lives in Berlin"]


def test_parse_facts_keeps_no_fact_when_the_preambled_array_is_empty():
    assert _parse_facts("Nothing worth keeping:\n```json\n[]\n```", 5) == []


@pytest.mark.parametrize(
    "junk", ["```", "```json", "[]", "{}", "[", "]", "---", ",", "JSON", '"facts":']
)
def test_parse_facts_never_stores_structural_debris(junk):
    assert _parse_facts(junk, 5) == []


def test_parse_facts_drops_debris_but_keeps_real_lines():
    raw = "```json\n- Likes tea\n]\n```"
    assert _contents(_parse_facts(raw, 5)) == ["Likes tea"]


# --- extract_facts ---

async def test_extract_facts_uses_configured_model_and_parses():
    runner = FakeRunner('["Is learning Spanish"]')
    settings = _settings(memory_tier="cheap")

    facts = await extract_facts(
        "I'm learning Spanish", "That's great!", settings=settings, runner=runner
    )

    assert _contents(facts) == ["Is learning Spanish"]
    assert runner.calls[0]["system"]  # the extraction prompt was supplied


async def test_extract_facts_attributes_spend_to_the_conversation():
    """The brain passes conversation_id; the writer used to drop it, so its spend
    was unattributable to the session that caused it."""
    runner = FakeRunner("[]")

    await extract_facts(
        "hi", "hello", settings=_settings(), runner=runner, conversation_id="sess-1"
    )

    assert runner.calls[0]["conversation_id"] == "sess-1"


async def test_extract_facts_short_circuits_on_empty_input():
    runner = FakeRunner('["should not be used"]')
    facts = await extract_facts("", "a reply", settings=_settings(), runner=runner)
    assert facts == []
    assert runner.calls == []  # no LLM call made


# --- remember ---

async def test_remember_stores_new_facts_and_logs_exchange(store, monkeypatch):
    async def fake_extract(user_text, assistant_text, known=(), **kw):
        return ["Likes hiking", "Has a dog named Mango"]

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    stored = await remember(
        "I hiked with my dog Mango", "Sounds fun!",
        store=store, settings=_settings(),
    )

    assert set(stored) == {"Likes hiking", "Has a dog named Mango"}
    assert store.fact_count() == 2
    # The raw exchange is logged regardless.
    assert [m["role"] for m in store.recent_messages(10)] == ["user", "assistant"]


async def test_remember_dedupes_against_existing(store, monkeypatch):
    """A repeat isn't 'stored' — it reinforces the row that already says it."""
    store.add_fact("Likes hiking")

    async def fake_extract(user_text, assistant_text, known=(), **kw):
        return ["Likes hiking", "New fact"]

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    stored = await remember("x", "y", store=store, settings=_settings())

    assert stored == ["New fact"]  # only the genuinely new one is reported
    assert store.fact_count() == 2
    # ...and the repeat left evidence that it came up again.
    hiking = next(f for f in store.all_facts() if f["content"] == "Likes hiking")
    assert hiking["use_count"] == 1


async def test_remember_supersedes_a_contradicted_fact(store, monkeypatch):
    store.add_fact("Lives in Boston")

    async def fake_extract(user_text, assistant_text, known=(), **kw):
        return [{"fact": "Lives in Denver", "replaces": "Lives in Boston"}]

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    stored = await remember("I moved to Denver", "Nice!", store=store, settings=_settings())

    assert stored == ["Lives in Denver"]
    # The old fact is gone from the active set, but still on record, pointing at
    # its replacement — so a bad correction is recoverable.
    assert [f["content"] for f in store.all_facts()] == ["Lives in Denver"]
    old = store.get_fact(1)
    assert old["status"] == "superseded"
    assert old["superseded_by"] == store.all_facts()[0]["id"]


async def test_remember_passes_known_facts_to_extractor(store, monkeypatch):
    store.add_fact("Already known")
    seen_known = {}

    async def fake_extract(user_text, assistant_text, known=(), **kw):
        seen_known["known"] = list(known)
        return []

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    await remember("x", "y", store=store, settings=_settings())
    assert "Already known" in seen_known["known"]


async def test_remember_shows_the_extractor_relevant_facts_not_just_recent(
    store, monkeypatch
):
    """The known-facts list is searched, not sampled.

    It used to be the N most recently stored facts, so once the store outgrew N the
    extractor was blind to everything older and re-proposed near-duplicates of facts
    it couldn't see.
    """
    store.add_fact("Has a dog named Mango")
    for i in range(12):
        store.add_fact(f"unrelated filler fact {i}")

    seen_known = {}

    async def fake_extract(user_text, assistant_text, known=(), **kw):
        seen_known["known"] = list(known)
        return []

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    await remember(
        "my dog Mango needs a walk", "Sounds good.",
        store=store, settings=_settings(memory_max_facts=5),
    )

    # The relevant old fact surfaced despite 12 newer ones burying it.
    assert "Has a dog named Mango" in seen_known["known"]


async def test_remember_logs_the_exchange_even_when_extraction_fails(store, monkeypatch):
    """Logging comes first now.

    It used to run after extraction, so a failed extraction — or a barge-in
    cancelling this coroutine — lost the raw conversation record too.
    """
    async def boom(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(writer, "extract_facts", boom)

    with pytest.raises(RuntimeError):
        await remember("what's the weather", "It's sunny.", store=store, settings=_settings())

    assert [m["content"] for m in store.recent_messages(10)] == [
        "what's the weather",
        "It's sunny.",
    ]


async def test_remember_drops_a_rambling_fact(store, monkeypatch):
    async def fake_extract(user_text, assistant_text, known=(), **kw):
        return ["x" * 500, "Likes tea"]

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    stored = await remember("x", "y", store=store, settings=_settings())
    assert stored == ["Likes tea"]


async def test_remember_noop_when_memory_disabled(store, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("extract_facts must not run when memory is off")

    monkeypatch.setattr(writer, "extract_facts", boom)

    stored = await remember(
        "x", "y", store=store, settings=_settings(feature_memory=False)
    )
    assert stored == []
    assert store.fact_count() == 0
