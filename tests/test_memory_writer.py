"""Tests for the memory writer — fact parsing, extraction, and persistence.

The LLM is always faked: either a fake Anthropic client (for `extract_facts`) or a
monkeypatched `extract_facts` (for `remember`). No network.
"""

import pytest

import app.memory.writer as writer
from app.config import Settings
from app.memory.store import MemoryStore
from app.memory.writer import _parse_facts, extract_facts, remember


# --- fake Anthropic client ---

class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeMessages:
    def __init__(self, text):
        self._text = text
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _Resp(self._text)


class FakeClient:
    def __init__(self, text):
        self.messages = FakeMessages(text)


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

def test_parse_facts_plain_json_array():
    assert _parse_facts('["Likes tea", "Has a cat"]', 5) == ["Likes tea", "Has a cat"]


def test_parse_facts_strips_code_fence():
    raw = '```json\n["Lives in Berlin"]\n```'
    assert _parse_facts(raw, 5) == ["Lives in Berlin"]


def test_parse_facts_falls_back_to_lines():
    raw = "- Likes tea\n- Has a cat"
    assert _parse_facts(raw, 5) == ["Likes tea", "Has a cat"]


def test_parse_facts_empty_array():
    assert _parse_facts("[]", 5) == []


def test_parse_facts_drops_none_sentinels():
    assert _parse_facts('["none"]', 5) == []


def test_parse_facts_respects_limit():
    raw = '["a", "b", "c", "d"]'
    assert _parse_facts(raw, 2) == ["a", "b"]


# --- extract_facts ---

async def test_extract_facts_uses_configured_model_and_parses():
    client = FakeClient('["Is learning Spanish"]')
    settings = _settings(memory_model="claude-test-model")

    facts = await extract_facts(
        "I'm learning Spanish", "That's great!", settings=settings, client=client
    )

    assert facts == ["Is learning Spanish"]
    assert client.messages.last_kwargs["model"] == "claude-test-model"


async def test_extract_facts_short_circuits_on_empty_input():
    client = FakeClient('["should not be used"]')
    facts = await extract_facts("", "a reply", settings=_settings(), client=client)
    assert facts == []
    assert client.messages.last_kwargs is None  # no LLM call made


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
    store.add_fact("Likes hiking")

    async def fake_extract(user_text, assistant_text, known=(), **kw):
        return ["Likes hiking", "New fact"]

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    stored = await remember("x", "y", store=store, settings=_settings())

    assert stored == ["New fact"]  # the duplicate was not re-stored
    assert store.fact_count() == 2


async def test_remember_passes_known_facts_to_extractor(store, monkeypatch):
    store.add_fact("Already known")
    seen_known = {}

    async def fake_extract(user_text, assistant_text, known=(), **kw):
        seen_known["known"] = list(known)
        return []

    monkeypatch.setattr(writer, "extract_facts", fake_extract)

    await remember("x", "y", store=store, settings=_settings())
    assert "Already known" in seen_known["known"]


async def test_remember_noop_when_memory_disabled(store, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("extract_facts must not run when memory is off")

    monkeypatch.setattr(writer, "extract_facts", boom)

    stored = await remember(
        "x", "y", store=store, settings=_settings(feature_memory=False)
    )
    assert stored == []
    assert store.fact_count() == 0
