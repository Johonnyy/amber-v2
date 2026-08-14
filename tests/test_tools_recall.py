"""Tests for the recall_recent tool — reads recent durable conversation messages."""

import pytest

import app.config as config_module
import app.tools.recall as recall
from app.memory.store import MemoryStore
from app.tools.registry import registry


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore(":memory:")
    monkeypatch.setattr(recall, "get_store", lambda: s)
    yield s
    s.close()


async def test_recall_returns_recent_messages_oldest_first(store):
    store.log_exchange("what's the capital of France?", "Paris.")
    store.log_exchange("and Italy?", "Rome.")
    out = await recall.recall_recent()
    assert "Paris" in out and "Rome" in out
    # Oldest-to-newest, so the France exchange precedes the Italy one.
    assert out.index("France") < out.index("Italy")


async def test_recall_empty_store(store):
    out = await recall.recall_recent()
    assert "no earlier conversations" in out.lower()


def test_recall_is_registered():
    assert "recall_recent" in registry.names()


def test_recall_hidden_when_memory_off(monkeypatch):
    monkeypatch.setenv("AMBER_FEATURE_MEMORY", "false")
    config_module.get_settings.cache_clear()
    try:
        assert "recall_recent" not in [t["name"] for t in registry.schemas()]
    finally:
        config_module.get_settings.cache_clear()


def test_recall_visible_when_memory_on(monkeypatch):
    monkeypatch.setenv("AMBER_FEATURE_MEMORY", "true")
    config_module.get_settings.cache_clear()
    try:
        assert "recall_recent" in [t["name"] for t in registry.schemas()]
    finally:
        config_module.get_settings.cache_clear()
