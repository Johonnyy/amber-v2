"""SQLite store — the persistent home of Amber's memory.

Tables:

* ``facts`` — distilled, punchy things worth remembering about the user
  (preferences, identity, ongoing context, patterns). This is what the context
  builder injects into the prompt. Kept small and high-signal on purpose.
* ``conversations`` — a durable log of exchanges (one row per message). Not
  replayed wholesale into prompts; it's the raw record the writer distils *from*
  and the substrate `recall_recent` searches.
* ``tasks`` / ``reminders`` — open/done items behind the Phase-4 tools.
* ``turn_events`` — lightweight telemetry (tool calls, barge-ins, corrections)
  the maintenance pass reflects on.
* ``reflections`` — the maintenance pass's own notes about how Amber is doing.

**Facts are tiered.** Every fact carries a ``tier`` (``session`` / ``short`` /
``durable``), a ``confidence``, a ``status``, and usage counters. That is what lets
memory curate itself: repeated usefulness promotes a fact, disuse decays one, and a
correction supersedes rather than duplicates. The shape is deliberately flat enough
to export to one markdown file per durable fact when long-term memory lands.

**Migrations.** The schema changes over time, so there is a real (if small)
migration runner: an ``amber_schema_version`` table plus an ordered list of
migration functions. It's a table rather than ``PRAGMA user_version`` because this
database file is shared three ways — Amber's memory, `agent_mcp`'s usage log, and
`agent_runtime`'s cost rows — and ``user_version`` is a single file-wide slot none
of us can claim safely.

**FTS5 is optional.** Fact and message search use an FTS5 index when the host's
SQLite provides one (capability-detected at open, since the VPS build may differ
from the dev box). Without it, callers fall back to ``LIKE``-based search — worse
ranking, same shape, never a crash.

This layer is **synchronous** sqlite3. The async-facing memory code (`writer`,
`context`) wraps these calls in ``asyncio.to_thread`` so a DB hit never blocks the
event loop / voice pipeline. A single connection is shared (``check_same_thread=
False``) and every call is serialized under a lock, which is plenty for one user.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from app.config import get_settings

logger = logging.getLogger(__name__)

# Fact lifecycle vocabularies, named once so callers never spell them by hand.
TIER_SESSION = "session"
TIER_SHORT = "short"
TIER_DURABLE = "durable"
TIERS = (TIER_SESSION, TIER_SHORT, TIER_DURABLE)

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_FORGOTTEN = "forgotten"

SOURCE_EXTRACTED = "extracted"
SOURCE_EXPLICIT = "explicit"
SOURCE_CONSOLIDATED = "consolidated"


# --- migrations -------------------------------------------------------------

# v1: the original Phase-3 schema. Kept verbatim so an existing amber.db created
# before the migration runner existed stamps cleanly at version 1 without
# re-running anything (every statement is IF NOT EXISTS).
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT    NOT NULL,
    category   TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);
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

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    remind_at  TEXT,                                -- ISO-8601, or NULL
    status     TEXT    NOT NULL DEFAULT 'pending',  -- 'pending' | 'done'
    created_at TEXT    NOT NULL
);
"""


def _m1_baseline(conn: sqlite3.Connection) -> None:
    conn.executescript(_V1_SCHEMA)


def _m2_fact_metadata(conn: sqlite3.Connection) -> None:
    """Give facts a lifecycle: tier, confidence, status, usage, provenance.

    Every column has a default so existing rows backfill in place. They land as
    ``short``/``extracted``, which is honest — nothing about them was ever
    confirmed by use.
    """
    for ddl in (
        f"ALTER TABLE facts ADD COLUMN tier TEXT NOT NULL DEFAULT '{TIER_SHORT}'",
        "ALTER TABLE facts ADD COLUMN confidence REAL NOT NULL DEFAULT 0.6",
        f"ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT '{STATUS_ACTIVE}'",
        "ALTER TABLE facts ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE facts ADD COLUMN last_used_at TEXT",
        "ALTER TABLE facts ADD COLUMN superseded_by INTEGER",
        f"ALTER TABLE facts ADD COLUMN source TEXT DEFAULT '{SOURCE_EXTRACTED}'",
    ):
        conn.execute(ddl)

    # The unique index has to go: a *forgotten* fact would otherwise be permanently
    # un-relearnable, since the row still exists. Dedup moves into add_fact, which
    # can scope it to active rows and reinforce instead of silently dropping.
    conn.execute("DROP INDEX IF EXISTS idx_facts_content_nocase")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_content_nocase "
        "ON facts (content COLLATE NOCASE)"
    )

    # Grandfather everything that predates tiering as durable, even though the
    # column default for *new* facts is 'short'. These rows have use_count 0 and no
    # last_used_at, so under the decay rules below they would all be forgotten on
    # the first maintenance run — silently wiping the memory this feature exists to
    # improve. They survived the pre-decay era; that's evidence enough.
    conn.execute(f"UPDATE facts SET tier = '{TIER_DURABLE}'")


def _m3_indexes(conn: sqlite3.Connection) -> None:
    """The database had exactly one index; every status/time query was a scan."""
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_facts_status_tier ON facts (status, tier)",
        "CREATE INDEX IF NOT EXISTS idx_facts_last_used ON facts (last_used_at)",
        "CREATE INDEX IF NOT EXISTS idx_conversations_created "
        "ON conversations (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status)",
        "CREATE INDEX IF NOT EXISTS idx_reminders_status_due "
        "ON reminders (status, remind_at)",
    ):
        conn.execute(ddl)


def _m4_events_and_reflections(conn: sqlite3.Connection) -> None:
    """Telemetry + the maintenance pass's notes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS turn_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT    NOT NULL,
            session_id TEXT,
            kind       TEXT    NOT NULL,  -- tool_call | barge_in | correction | turn
            name       TEXT,              -- tool name, modality, ...
            ok         INTEGER,           -- 1/0/NULL where meaningful
            latency_ms INTEGER,
            detail     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turn_events_created
            ON turn_events (created_at);
        CREATE INDEX IF NOT EXISTS idx_turn_events_kind ON turn_events (kind);

        CREATE TABLE IF NOT EXISTS reflections (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT    NOT NULL,
            period_start TEXT,
            period_end   TEXT,
            kind         TEXT    NOT NULL DEFAULT 'note',
            note         TEXT    NOT NULL,
            dismissed    INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def _m5_fts(conn: sqlite3.Connection) -> None:
    """Full-text indexes over facts and messages, with sync triggers.

    External-content tables, so the text is stored once. Raises
    ``sqlite3.OperationalError`` on a build without FTS5 — the caller catches that
    and leaves ``fts_enabled`` false.
    """
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
            content, content='facts', content_rowid='id',
            tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
            INSERT INTO facts_fts (rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
            INSERT INTO facts_fts (facts_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
            INSERT INTO facts_fts (facts_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            INSERT INTO facts_fts (rowid, content) VALUES (new.id, new.content);
        END;

        CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
            content, content='conversations', content_rowid='id'
        );
        CREATE TRIGGER IF NOT EXISTS conversations_ai
        AFTER INSERT ON conversations BEGIN
            INSERT INTO conversations_fts (rowid, content)
                VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS conversations_ad
        AFTER DELETE ON conversations BEGIN
            INSERT INTO conversations_fts (conversations_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
        END;
        """
    )
    # Backfill anything that predates the index.
    conn.execute("INSERT INTO facts_fts (facts_fts) VALUES ('rebuild')")
    conn.execute("INSERT INTO conversations_fts (conversations_fts) VALUES ('rebuild')")


# Ordered; index + 1 is the schema version each one produces. Append only.
_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _m1_baseline,
    _m2_fact_metadata,
    _m3_indexes,
    _m4_events_and_reflections,
    _m5_fts,
)

# The FTS migration is allowed to fail (old SQLite); the rest are not.
_OPTIONAL_MIGRATIONS = frozenset({_m5_fts.__name__})


def _tier_rank(tier: str | None) -> int:
    """Where a tier sits in the durability order. Unknown tiers sort lowest."""
    try:
        return TIERS.index(tier or "")
    except ValueError:
        return -1


def _now() -> str:
    """Current UTC time as an ISO-8601 string (lexically sortable)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


_FACT_FIELDS = (
    "id",
    "content",
    "category",
    "tier",
    "confidence",
    "status",
    "use_count",
    "last_used_at",
    "superseded_by",
    "source",
    "created_at",
    "updated_at",
)
_FACT_COLUMNS = ", ".join(_FACT_FIELDS)
_FACT_COLUMNS_F = ", ".join(f"f.{c}" for c in _FACT_FIELDS)

# FTS5 query syntax is a real grammar — user utterances are not. Reduce an
# utterance to bare alphanumeric terms and OR them, so punctuation, quotes and
# operators can never produce a syntax error mid-turn.
_FTS_TERM_RE = re.compile(r"[A-Za-z0-9]+")

# Words that match everything and therefore rank nothing. Without this, "tell me
# about the dog" scores a fact containing "the" as a hit, and common words drown the
# one term that actually carried the question.
_STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have
    he her him his how i if in into is it its just like me my no not of on or our out
    she so than that the their them then there these they this to too up us was we
    were what when where which who why will with would you your""".split()
)


def _fts_query(text: str) -> str:
    """An utterance as a safe FTS5 MATCH expression, or "" if there's nothing to ask.

    Every term is quoted, which is what makes this safe rather than merely tidy:
    ``AND``, ``OR``, ``NOT`` and ``NEAR`` are FTS5 operators, so an unquoted term
    list built from ordinary speech ("the dog and the cat") is a syntax error, not a
    search. Quoted, each term is just a string to look for.
    """
    terms = _search_terms(text)
    return " OR ".join(f'"{t}"' for t in terms)


def _search_terms(text: str) -> list[str]:
    """The words worth searching for in an utterance."""
    return [
        t
        for t in _FTS_TERM_RE.findall((text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


class MemoryStore:
    """Thin synchronous wrapper over the SQLite memory database."""

    def __init__(self, path: str = "amber.db") -> None:
        self.path = path
        # check_same_thread=False: the connection is reused from asyncio.to_thread
        # worker threads. Safe here because every access is serialized by _lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.fts_enabled = False
        with self._lock:
            self._configure()
            self._migrate()

    def _configure(self) -> None:
        """Apply the pragmas this database file's tenants agree on.

        ``amber.db`` holds three schemas — Amber's memory, `agent_mcp`'s usage log,
        and `agent_runtime`'s cost rows — written by three independent code paths.
        `agent_mcp` documents the contract (prefix your tables, WAL, a busy timeout,
        ISO-8601 text timestamps) and this store was the one tenant not honouring
        it, which is a "database is locked" waiting to happen the moment a
        maintenance pass and a turn overlap. All three pragmas are idempotent, and
        WAL is a persistent property of the file, so whoever opens first sets it.
        """
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=5000",
            "PRAGMA foreign_keys=ON",
        ):
            try:
                self._conn.execute(pragma)
            except sqlite3.OperationalError:
                # ":memory:" refuses WAL; never worth failing an open over.
                logger.debug("Pragma not applied: %s", pragma)

    # --- schema ---

    def _migrate(self) -> None:
        """Bring the database up to the latest schema version. Caller holds the lock."""
        conn = self._conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS amber_schema_version ("
            "  version INTEGER NOT NULL,"
            "  applied_at TEXT NOT NULL"
            ")"
        )
        row = conn.execute(
            "SELECT MAX(version) AS v FROM amber_schema_version"
        ).fetchone()
        current = int(row["v"] or 0)

        for index, migration in enumerate(_MIGRATIONS):
            version = index + 1
            if version <= current:
                continue
            optional = migration.__name__ in _OPTIONAL_MIGRATIONS
            try:
                migration(conn)
            except sqlite3.OperationalError:
                if not optional:
                    raise
                conn.rollback()
                logger.warning(
                    "Skipping optional migration v%d (%s): this SQLite build lacks "
                    "the feature. Search degrades to LIKE matching.",
                    version,
                    migration.__name__,
                )
                # Still stamp it: re-attempting every open would just re-log. A
                # rebuilt host with FTS5 gets it via a future migration.
            conn.execute(
                "INSERT INTO amber_schema_version (version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )
            conn.commit()

        self.fts_enabled = self._detect_fts()

    def _detect_fts(self) -> bool:
        """True if the FTS tables exist and are queryable. Caller holds the lock."""
        try:
            self._conn.execute("SELECT 1 FROM facts_fts LIMIT 1").fetchone()
            self._conn.execute("SELECT 1 FROM conversations_fts LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM amber_schema_version"
            ).fetchone()
        return int(row["v"] or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- facts ---

    def add_fact(
        self,
        content: str,
        category: str | None = None,
        *,
        tier: str = TIER_SHORT,
        confidence: float = 0.6,
        source: str = SOURCE_EXTRACTED,
    ) -> int | None:
        """Insert a distilled fact, or reinforce the one that already says it.

        Returns the row id either way, or ``None`` for blank content. When the same
        text is already active, the existing fact is *strengthened* rather than
        dropped — a repeat mention is the clearest evidence a fact matters, so its
        use count rises, its confidence edges up, and an explicit re-assertion can
        promote it to a higher tier. Callers that need to tell the two cases apart
        use `fact_exists` first, or check ``use_count``.
        """
        content = content.strip()
        if not content:
            return None
        now = _now()
        with self._lock:
            existing = self._conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts "
                "WHERE content = ? COLLATE NOCASE AND status = ? LIMIT 1",
                (content, STATUS_ACTIVE),
            ).fetchone()

            if existing is not None:
                # Never demote: an explicit "remember this" can raise a short fact
                # to durable, but a background extraction can't push one back down.
                new_tier = (
                    tier if _tier_rank(tier) > _tier_rank(existing["tier"]) else existing["tier"]
                )
                self._conn.execute(
                    "UPDATE facts SET use_count = use_count + 1, "
                    "confidence = MIN(1.0, MAX(confidence, ?) + 0.05), "
                    "tier = ?, updated_at = ? WHERE id = ?",
                    (confidence, new_tier, now, existing["id"]),
                )
                self._conn.commit()
                return int(existing["id"])

            cur = self._conn.execute(
                "INSERT INTO facts (content, category, tier, confidence, status, "
                "use_count, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (content, category, tier, confidence, STATUS_ACTIVE, source, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def fact_exists(self, content: str) -> bool:
        """True if an *active* fact already says exactly this (case-insensitively)."""
        content = (content or "").strip()
        if not content:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM facts WHERE content = ? COLLATE NOCASE AND status = ? "
                "LIMIT 1",
                (content, STATUS_ACTIVE),
            ).fetchone()
        return row is not None

    def get_fact(self, fact_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
        return dict(row) if row else None

    def all_facts(self) -> list[dict]:
        """Every *active* fact, newest first.

        Unbounded by design for maintenance and export; the per-turn read path uses
        `search_facts` / `active_facts` instead so it never scans the whole table.
        """
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts WHERE status = ? ORDER BY id DESC",
                (STATUS_ACTIVE,),
            ).fetchall()
        return [dict(r) for r in rows]

    def active_facts(
        self,
        *,
        tier: str | None = None,
        limit: int | None = None,
        order: str = "recent",
    ) -> list[dict]:
        """Active facts, optionally restricted to one tier.

        ``order`` is ``"recent"`` (newest first) or ``"used"`` (most-used first,
        which is what the always-include durable slice wants).
        """
        if limit is not None and limit <= 0:
            return []
        sql = f"SELECT {_FACT_COLUMNS} FROM facts WHERE status = ?"
        params: list = [STATUS_ACTIVE]
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        sql += (
            " ORDER BY use_count DESC, id DESC"
            if order == "used"
            else " ORDER BY id DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def recent_facts(self, limit: int) -> list[dict]:
        """The ``limit`` most recently stored active facts, newest first."""
        return self.active_facts(limit=limit)

    def search_facts(self, query: str, limit: int = 20) -> list[dict]:
        """Active facts matching ``query``, best match first.

        FTS5 ``bm25`` ranking when the index exists, ``LIKE`` on each term when it
        doesn't. Returns ``[]`` for an empty query rather than everything — callers
        that want "everything" ask for it explicitly.
        """
        if limit <= 0:
            return []
        match = _fts_query(query)
        if not match:
            return []

        if self.fts_enabled:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {_FACT_COLUMNS_F}, bm25(facts_fts) AS rank "
                    "FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                    "WHERE facts_fts MATCH ? AND f.status = ? "
                    "ORDER BY rank LIMIT ?",
                    (match, STATUS_ACTIVE, limit),
                ).fetchall()
            return [dict(r) for r in rows]

        # Fallback: OR of LIKE clauses, newest first. No ranking, but the caller's
        # scorer does the real work anyway.
        terms = _search_terms(query)
        if not terms:
            return []
        where = " OR ".join("content LIKE ?" for _ in terms)
        params: list = [f"%{t}%" for t in terms]
        params.append(STATUS_ACTIVE)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts "
                f"WHERE ({where}) AND status = ? ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_facts(self, fact_ids: Sequence[int]) -> None:
        """Record that these facts were actually used in a completed turn.

        This is the only evidence memory has that a fact earns its tokens, so it
        drives both promotion and decay. Called *off* the latency path.
        """
        ids = [int(i) for i in fact_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._conn.execute(
                "UPDATE facts SET use_count = use_count + 1, last_used_at = ? "
                f"WHERE id IN ({placeholders})",
                [_now(), *ids],
            )
            self._conn.commit()

    def update_fact(
        self,
        fact_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
        tier: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
    ) -> bool:
        """Patch a fact in place. Returns ``True`` if a row changed."""
        sets: list[str] = []
        params: list = []
        for column, value in (
            ("content", content.strip() if isinstance(content, str) else None),
            ("category", category),
            ("tier", tier),
            ("confidence", confidence),
            ("status", status),
        ):
            if value is not None and value != "":
                sets.append(f"{column} = ?")
                params.append(value)
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.extend([_now(), fact_id])
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE facts SET {', '.join(sets)} WHERE id = ?", params
            )
            self._conn.commit()
            return cur.rowcount > 0

    def supersede_fact(
        self,
        old_id: int,
        content: str,
        *,
        category: str | None = None,
        tier: str = TIER_DURABLE,
        confidence: float = 0.9,
        source: str = SOURCE_EXPLICIT,
    ) -> int | None:
        """Replace a fact with a corrected one, keeping the link between them.

        The old row stays (``status='superseded'``, ``superseded_by`` pointing at
        the replacement) rather than being deleted, so a wrong correction is
        recoverable and the maintenance pass can see what changed.
        """
        content = content.strip()
        if not content:
            return None
        now = _now()
        with self._lock:
            old = self._conn.execute(
                "SELECT id, category FROM facts WHERE id = ?", (old_id,)
            ).fetchone()
            if old is None:
                return None
            cur = self._conn.execute(
                "INSERT INTO facts (content, category, tier, confidence, status, "
                "use_count, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    content,
                    category if category is not None else old["category"],
                    tier,
                    confidence,
                    STATUS_ACTIVE,
                    source,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid)
            self._conn.execute(
                "UPDATE facts SET status = ?, superseded_by = ?, updated_at = ? "
                "WHERE id = ?",
                (STATUS_SUPERSEDED, new_id, now, old_id),
            )
            self._conn.commit()
            return new_id

    def forget_fact(self, fact_id: int) -> dict | None:
        """Soft-delete a fact. Returns the row as it was, or ``None`` if not active.

        Soft, not hard: the content is what the caller echoes back ("forgotten:
        ...") and keeping the row means a mistaken forget is recoverable.
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = ? AND status = ?",
                (fact_id, STATUS_ACTIVE),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE facts SET status = ?, updated_at = ? WHERE id = ?",
                (STATUS_FORGOTTEN, _now(), fact_id),
            )
            self._conn.commit()
        return dict(row)

    def fact_count(self, *, status: str | None = STATUS_ACTIVE) -> int:
        sql = "SELECT COUNT(*) FROM facts"
        params: tuple = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        with self._lock:
            return int(self._conn.execute(sql, params).fetchone()[0])

    # --- fact lifecycle (driven by the maintenance pass) ---

    def promote_facts(self, min_uses: int) -> int:
        """Promote short-tier facts that keep proving useful. Returns rows changed.

        Repeated usefulness is the only evidence of durability that doesn't need a
        model, so this runs as plain SQL.
        """
        if min_uses <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET tier = ?, updated_at = ? "
                "WHERE status = ? AND tier = ? AND use_count >= ?",
                (TIER_DURABLE, _now(), STATUS_ACTIVE, TIER_SHORT, min_uses),
            )
            self._conn.commit()
            return cur.rowcount

    def decay_facts(
        self, *, short_ttl_days: float, session_ttl_hours: float
    ) -> int:
        """Forget stale, unused low-tier facts. Returns rows changed.

        Durable facts are never touched here — they leave only by being superseded
        or explicitly forgotten. A short fact survives if it has been used at least
        twice, however old it is.
        """
        now = _now()
        changed = 0
        with self._lock:
            if session_ttl_hours > 0:
                cutoff = _iso_hours_ago(session_ttl_hours)
                cur = self._conn.execute(
                    "UPDATE facts SET status = ?, updated_at = ? "
                    "WHERE status = ? AND tier = ? "
                    "AND COALESCE(last_used_at, created_at) < ?",
                    (STATUS_FORGOTTEN, now, STATUS_ACTIVE, TIER_SESSION, cutoff),
                )
                changed += cur.rowcount
            if short_ttl_days > 0:
                cutoff = _iso_days_ago(short_ttl_days)
                cur = self._conn.execute(
                    "UPDATE facts SET status = ?, updated_at = ? "
                    "WHERE status = ? AND tier = ? AND use_count < 2 "
                    "AND COALESCE(last_used_at, created_at) < ?",
                    (STATUS_FORGOTTEN, now, STATUS_ACTIVE, TIER_SHORT, cutoff),
                )
                changed += cur.rowcount
            self._conn.commit()
        return changed

    def facts_changed_since(self, since_iso: str, limit: int) -> list[dict]:
        """Active facts created or updated since ``since_iso``, oldest first.

        The maintenance pass consolidates only these (plus their search
        neighbours), so its cost stays proportional to what actually changed.
        """
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts "
                "WHERE status = ? AND updated_at >= ? ORDER BY updated_at ASC LIMIT ?",
                (STATUS_ACTIVE, since_iso, limit),
            ).fetchall()
        return [dict(r) for r in rows]

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

    def search_messages(self, query: str, limit: int = 12) -> list[dict]:
        """Logged messages matching ``query``, oldest first so they read as a thread."""
        if limit <= 0:
            return []
        match = _fts_query(query)
        if not match:
            return []

        if self.fts_enabled:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT c.id, c.role, c.content, c.created_at "
                    "FROM conversations_fts "
                    "JOIN conversations c ON c.id = conversations_fts.rowid "
                    "WHERE conversations_fts MATCH ? "
                    "ORDER BY bm25(conversations_fts) LIMIT ?",
                    (match, limit),
                ).fetchall()
        else:
            terms = _search_terms(query)
            if not terms:
                return []
            where = " OR ".join("content LIKE ?" for _ in terms)
            params: list = [f"%{t}%" for t in terms]
            params.append(limit)
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, role, content, created_at FROM conversations "
                    f"WHERE {where} ORDER BY id DESC LIMIT ?",
                    params,
                ).fetchall()
        return [dict(r) for r in sorted(rows, key=lambda r: r["id"])]

    def prune_conversations(self, keep_days: float) -> int:
        """Drop logged messages older than ``keep_days``. Returns rows removed."""
        if keep_days <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conversations WHERE created_at < ?",
                (_iso_days_ago(keep_days),),
            )
            self._conn.commit()
            return cur.rowcount

    # --- tasks ---

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

    def open_tasks(self, limit: int | None = None) -> list[dict]:
        """Open tasks, oldest first (the order you'd work through them)."""
        sql = (
            "SELECT id, description, status, created_at, completed_at "
            "FROM tasks WHERE status = 'open' ORDER BY id ASC"
        )
        params: tuple = ()
        if limit is not None:
            if limit <= 0:
                return []
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
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

    # --- reminders ---

    def add_reminder(self, text: str, remind_at: str | None = None) -> int:
        """Persist a reminder. ``remind_at`` is an ISO-8601 time, or ``None`` if
        the user gave no time. Delivery/firing is still future work (it needs a
        server-initiated push frame); this durably records the intent."""
        text = text.strip()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (text, remind_at, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (text, remind_at, _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def pending_reminders(self) -> list[dict]:
        """Reminders not yet delivered, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, remind_at, status, created_at FROM reminders "
                "WHERE status = 'pending' ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def due_reminders(self, now_iso: str | None = None) -> list[dict]:
        """Pending reminders whose time has arrived, oldest due first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, remind_at, status, created_at FROM reminders "
                "WHERE status = 'pending' AND remind_at IS NOT NULL AND remind_at <= ? "
                "ORDER BY remind_at ASC",
                (now_iso or _now(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def complete_reminder(self, reminder_id: int) -> bool:
        """Mark a reminder done. Returns ``True`` if a pending one was updated."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reminders SET status = 'done' WHERE id = ? AND status = 'pending'",
                (reminder_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # --- telemetry ---

    def add_turn_events(self, events: Iterable[dict]) -> int:
        """Batch-insert telemetry rows. Returns how many landed.

        Batched because the writer drains a queue: one commit per flush, never one
        per event on the turn path.
        """
        rows = [
            (
                e.get("created_at") or _now(),
                e.get("session_id"),
                e.get("kind", "unknown"),
                e.get("name"),
                None if e.get("ok") is None else int(bool(e["ok"])),
                e.get("latency_ms"),
                e.get("detail"),
            )
            for e in events
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO turn_events "
                "(created_at, session_id, kind, name, ok, latency_ms, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def event_summary(self, since_iso: str) -> list[dict]:
        """Per (kind, name, ok) counts and median-ish latency since a timestamp."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, name, ok, COUNT(*) AS count, "
                "CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms, "
                "MAX(latency_ms) AS max_latency_ms "
                "FROM turn_events WHERE created_at >= ? "
                "GROUP BY kind, name, ok ORDER BY count DESC",
                (since_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def prune_turn_events(self, keep_days: float) -> int:
        if keep_days <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM turn_events WHERE created_at < ?",
                (_iso_days_ago(keep_days),),
            )
            self._conn.commit()
            return cur.rowcount

    # --- reflections ---

    def add_reflection(
        self,
        note: str,
        *,
        kind: str = "note",
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> int | None:
        note = (note or "").strip()
        if not note:
            return None
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reflections "
                "(created_at, period_start, period_end, kind, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (_now(), period_start, period_end, kind, note),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent_reflections(self, limit: int = 5) -> list[dict]:
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, period_start, period_end, kind, note "
                "FROM reflections WHERE dismissed = 0 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_reflection_at(self) -> str | None:
        """When the maintenance pass last wrote a note, for the next run's window."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(created_at) AS at FROM reflections"
            ).fetchone()
        return row["at"] if row and row["at"] else None


@lru_cache
def get_store() -> MemoryStore:
    """Process-wide store singleton, opened at the configured DB path.

    Cached so migrations run once and the connection is reused. Tests that want
    isolation construct ``MemoryStore`` directly (e.g. with ``":memory:"``) and pass
    it in, rather than going through this.
    """
    return MemoryStore(get_settings().memory_db_path)
