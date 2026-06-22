"""SQLite store — the persistent home of Amber's memory.

Three tables, matching the design spec:

* ``facts`` — distilled, punchy things worth remembering about the user
  (preferences, identity, ongoing context, patterns). This is what the context
  builder injects into the prompt. Kept small and high-signal on purpose.
* ``conversations`` — a durable log of exchanges (one row per message). Not
  replayed wholesale into prompts; it's the raw record the writer distils *from*
  and a substrate for future recall.
* ``tasks`` — open/done items. The schema and CRUD land here in Phase 3 so the
  Phase-4 task tools have a home; the context builder already surfaces open ones.

This layer is **synchronous** sqlite3. The async-facing memory code (`writer`,
`context`) wraps these calls in ``asyncio.to_thread`` so a DB hit never blocks the
event loop / voice pipeline. A single connection is shared (``check_same_thread=
False``) and every call is serialized under a lock, which is plenty for one user.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache

from app.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT    NOT NULL,
    category   TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);
-- Cheap exact-dedup guard: never store the same fact twice (case-insensitive).
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_content_nocase
    ON facts (content COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT    NOT NULL,   -- 'user' | 'assistant'
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    description  TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',  -- 'open' | 'done'
    created_at   TEXT    NOT NULL,
    completed_at TEXT
);
"""


def _now() -> str:
    """Current UTC time as an ISO-8601 string (lexically sortable)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    """Thin synchronous wrapper over the SQLite memory database."""

    def __init__(self, path: str = "amber.db") -> None:
        self.path = path
        # check_same_thread=False: the connection is reused from asyncio.to_thread
        # worker threads. Safe here because every access is serialized by _lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- facts ---

    def add_fact(self, content: str, category: str | None = None) -> int | None:
        """Insert a distilled fact. Returns its id, or ``None`` if a duplicate.

        Duplicates (same text, case-insensitive) are silently ignored so the
        writer can re-offer known facts without growing the store.
        """
        content = content.strip()
        if not content:
            return None
        now = _now()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO facts (content, category, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (content, category, now, now),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None  # unique-index collision: already known

    def all_facts(self) -> list[dict]:
        """Every fact, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, category, created_at, updated_at "
                "FROM facts ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_facts(self, limit: int) -> list[dict]:
        """The ``limit`` most recently stored facts, newest first."""
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content, category, created_at, updated_at "
                "FROM facts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def fact_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    # --- conversations (durable exchange log) ---

    def add_message(self, role: str, content: str) -> int:
        content = content.strip()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO conversations (role, content, created_at) VALUES (?, ?, ?)",
                (role, content, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def log_exchange(self, user_text: str, assistant_text: str) -> None:
        """Persist one full turn (user message + assistant reply)."""
        if user_text and user_text.strip():
            self.add_message("user", user_text)
        if assistant_text and assistant_text.strip():
            self.add_message("assistant", assistant_text)

    def recent_messages(self, limit: int) -> list[dict]:
        """The ``limit`` most recent logged messages, oldest first."""
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, created_at FROM conversations "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # --- tasks (schema + CRUD now; tools wire in Phase 4) ---

    def add_task(self, description: str) -> int:
        description = description.strip()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tasks (description, status, created_at) "
                "VALUES (?, 'open', ?)",
                (description, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def open_tasks(self) -> list[dict]:
        """Open tasks, oldest first (the order you'd work through them)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, description, status, created_at, completed_at "
                "FROM tasks WHERE status = 'open' ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def complete_task(self, task_id: int) -> bool:
        """Mark a task done. Returns ``True`` if a still-open task was updated."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? "
                "WHERE id = ? AND status = 'open'",
                (_now(), task_id),
            )
            self._conn.commit()
            return cur.rowcount > 0


@lru_cache
def get_store() -> MemoryStore:
    """Process-wide store singleton, opened at the configured DB path.

    Cached so the schema is applied once and the connection is reused. Tests that
    want isolation construct ``MemoryStore`` directly (e.g. with ``":memory:"``)
    and pass it in, rather than going through this.
    """
    return MemoryStore(get_settings().memory_db_path)
