"""Sessions — per-connection history plus the manager that lets it survive a drop.

Two layers live here:

* `Conversation` — the **short-term** "what we've been talking about" buffer that's
  replayed to the LLM each turn. Its message shape matches the Anthropic Messages
  API directly, so it can be handed straight to `app.brain.think`. Consecutive
  same-role turns are fine (the API combines them), which is why an interrupted
  turn that saved no assistant text is harmless. It is deliberately *not* the same
  as persistent memory (Phase 3, SQLite): cross-session knowledge belongs there.
  Phase 5 adds a size cap so a long-lived session's history — and the tokens it
  costs every turn — stays bounded.

* `Session` + `SessionManager` (Phase 5) — give each connection a stable **session
  id** and keep its `Conversation` alive for a TTL window after the socket drops,
  so a client that reconnects with its id resumes mid-conversation instead of
  starting cold. The manager also enforces memory guardrails: a cap on retained
  sessions (evicting the least-recently-active) and per-session rate limiting.

The manager is touched only from WS handlers on the one event loop, so it is
lock-free by design; ``clock`` is injectable for deterministic tests.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache

from app.config import get_settings
from app.ratelimit import RateLimiter


@dataclass
class Conversation:
    """In-memory history for one session.

    ``max_messages`` caps the retained history; when set, the oldest messages are
    dropped on append and the buffer is kept starting on a ``user`` turn (the
    Messages API requires the first message to be from the user). ``None`` (the
    default) keeps everything — handy for tests and single-turn use.
    """

    messages: list[dict] = field(default_factory=list)
    max_messages: int | None = None

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self._trim()

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
        self._trim()

    def _trim(self) -> None:
        if self.max_messages is None or len(self.messages) <= self.max_messages:
            return
        excess = len(self.messages) - self.max_messages
        del self.messages[:excess]
        # History must start on a user turn for the Messages API; if trimming left
        # a leading assistant turn, drop it too.
        while self.messages and self.messages[0]["role"] != "user":
            del self.messages[0]


@dataclass
class Session:
    """One client's identity + retained state, keyed by `id`."""

    id: str
    conversation: Conversation
    created_at: float
    last_active: float
    limiter: RateLimiter
    turns: int = 0  # total utterances admitted over this session's lifetime


class SessionManager:
    """Registry of live sessions with reconnect, TTL eviction, and capacity caps."""

    def __init__(
        self,
        *,
        ttl_s: float,
        max_sessions: int,
        max_messages: int | None,
        rate_limit_turns: int,
        rate_limit_window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_sessions = max_sessions
        self._max_messages = max_messages
        self._rate_limit_turns = rate_limit_turns
        self._rate_limit_window_s = rate_limit_window_s
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def resume_or_create(self, session_id: str | None) -> tuple[Session, bool]:
        """Resume a live session by id, or mint a fresh one.

        Returns ``(session, resumed)``. A presented id is honored only if it maps
        to a still-live (non-expired) session — unknown or expired ids get a brand
        new server-generated id, so a client can't seed or hijack arbitrary ids.
        """
        now = self._clock()
        self._evict_expired(now)

        if session_id:
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.last_active = now
                return existing, True

        session = Session(
            id=secrets.token_urlsafe(16),
            conversation=Conversation(max_messages=self._max_messages),
            created_at=now,
            last_active=now,
            limiter=RateLimiter(self._rate_limit_turns, self._rate_limit_window_s),
        )
        self._sessions[session.id] = session
        self._enforce_capacity()
        return session, False

    def touch(self, session: Session) -> None:
        """Mark a session active now (called on each turn and on disconnect so the
        TTL clock for the reconnect window restarts from the last interaction)."""
        session.last_active = self._clock()

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def count(self) -> int:
        return len(self._sessions)

    def _evict_expired(self, now: float) -> None:
        if self._ttl_s <= 0:
            return
        expired = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_active > self._ttl_s
        ]
        for sid in expired:
            del self._sessions[sid]

    def _enforce_capacity(self) -> None:
        if self._max_sessions <= 0:
            return
        while len(self._sessions) > self._max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.last_active)
            del self._sessions[oldest.id]


@lru_cache
def get_session_manager() -> SessionManager:
    """Process-wide session manager, built from settings.

    Cached so every connection shares one registry. Tests that need a different
    configuration clear the cache (``get_session_manager.cache_clear()``) after
    setting the environment, or construct a ``SessionManager`` directly.
    """
    s = get_settings()
    return SessionManager(
        ttl_s=s.session_ttl_s,
        max_sessions=s.max_sessions,
        max_messages=(s.max_history_turns * 2) if s.max_history_turns > 0 else None,
        rate_limit_turns=s.rate_limit_turns,
        rate_limit_window_s=s.rate_limit_window_s,
    )
