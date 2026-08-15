"""Tests for the schema migration runner.

The point of these is a single promise: **an ``amber.db`` written by an older Amber
must survive an upgrade with its contents intact.** Before the runner existed the
schema was applied as ``CREATE TABLE IF NOT EXISTS``, so any new column would simply
never reach a database that already had the table — which is why the migration
mechanism had to land before anything depended on it.

``_V1_SCHEMA`` is deliberately frozen in the source: it is what a pre-migration
database actually looks like, and these tests build one from it rather than
describing it second-hand.
"""

import sqlite3

import pytest

from app.memory.store import _MIGRATIONS, _V1_SCHEMA, MemoryStore

LATEST = len(_MIGRATIONS)


def _legacy_db(path):
    """A database exactly as the pre-migration Amber left it, with real rows."""
    conn = sqlite3.connect(path)
    conn.executescript(_V1_SCHEMA)
    conn.execute(
        "INSERT INTO facts (content, category, created_at, updated_at) "
        "VALUES ('Has a dog named Mango', NULL, '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO conversations (role, content, created_at) "
        "VALUES ('user', 'how is Mango', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO tasks (description, status, created_at) "
        "VALUES ('buy dog food', 'open', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    return path


def test_a_fresh_database_lands_on_the_latest_version(tmp_path):
    store = MemoryStore(str(tmp_path / "fresh.db"))
    try:
        assert store.schema_version == LATEST
    finally:
        store.close()


def test_a_legacy_database_keeps_its_rows(tmp_path):
    path = _legacy_db(str(tmp_path / "legacy.db"))

    store = MemoryStore(path)
    try:
        assert store.schema_version == LATEST
        assert [f["content"] for f in store.all_facts()] == ["Has a dog named Mango"]
        assert [m["content"] for m in store.recent_messages(10)] == ["how is Mango"]
        assert [t["description"] for t in store.open_tasks()] == ["buy dog food"]
    finally:
        store.close()


def test_legacy_facts_backfill_with_lifecycle_defaults(tmp_path):
    path = _legacy_db(str(tmp_path / "legacy.db"))

    store = MemoryStore(path)
    try:
        fact = store.all_facts()[0]
        assert fact["status"] == "active"
        assert fact["confidence"] == pytest.approx(0.6)
        assert fact["use_count"] == 0
        assert fact["superseded_by"] is None
        # Grandfathered as durable even though new facts default to 'short':
        # these rows have no usage history, so the decay pass would otherwise
        # forget every one of them on its first run.
        assert fact["tier"] == "durable"
    finally:
        store.close()


def test_new_facts_still_start_in_the_short_tier(tmp_path):
    """The grandfathering above is a one-time backfill, not the new default."""
    store = MemoryStore(str(tmp_path / "fresh.db"))
    try:
        fact_id = store.add_fact("Is learning Spanish")
        assert store.get_fact(fact_id)["tier"] == "short"
    finally:
        store.close()


def test_reopening_is_a_no_op(tmp_path):
    path = _legacy_db(str(tmp_path / "legacy.db"))

    first = MemoryStore(path)
    first.close()
    second = MemoryStore(path)
    try:
        assert second.schema_version == LATEST
        assert second.fact_count() == 1
        # No duplicate version stamps, and no re-run of the backfill.
        rows = second._conn.execute(
            "SELECT COUNT(*) FROM amber_schema_version"
        ).fetchone()[0]
        assert rows == LATEST
    finally:
        second.close()


def test_full_text_search_indexes_rows_that_predate_it(tmp_path):
    """The FTS migration backfills; facts written before it are still findable."""
    path = _legacy_db(str(tmp_path / "legacy.db"))

    store = MemoryStore(path)
    try:
        if not store.fts_enabled:
            pytest.skip("this SQLite build has no FTS5")
        assert [f["content"] for f in store.search_facts("Mango")] == [
            "Has a dog named Mango"
        ]
        assert [m["content"] for m in store.search_messages("Mango")] == ["how is Mango"]
    finally:
        store.close()


def test_search_still_works_without_full_text_search(tmp_path):
    """A SQLite build without FTS5 degrades to LIKE matching, never a crash.

    The dev box has FTS5 and the VPS may not, so the fallback needs a test that
    doesn't depend on which one is running it.
    """
    store = MemoryStore(str(tmp_path / "nofts.db"))
    try:
        store.add_fact("Has a dog named Mango")
        store.add_fact("Prefers tea over coffee")
        store.log_exchange("how is Mango", "He's well.")

        store.fts_enabled = False  # simulate the older build

        assert [f["content"] for f in store.search_facts("Mango")] == [
            "Has a dog named Mango"
        ]
        assert [m["content"] for m in store.search_messages("Mango")] == [
            "how is Mango"
        ]
        assert store.search_facts("") == []
    finally:
        store.close()


def test_a_hostile_utterance_cannot_break_the_search_query(tmp_path):
    """FTS5 MATCH is a grammar; an utterance is not. Terms are extracted, not passed."""
    store = MemoryStore(str(tmp_path / "fts.db"))
    try:
        store.add_fact("Has a dog named Mango")
        for query in ('"unclosed', "NEAR(", "dog AND OR NOT", "*", "a-b^c:d"):
            store.search_facts(query)  # must not raise
    finally:
        store.close()
