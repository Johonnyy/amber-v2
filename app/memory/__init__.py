"""Persistent memory (Phase 3) — what makes Amber feel like it knows you.

Memory is the cross-session picture of the user. It has three concerns, one per
submodule:

* `store` — the SQLite layer (`facts`, `conversations`, `tasks`) and all raw CRUD.
* `writer` — after each exchange, distil **punchy facts worth keeping** (not raw
  transcripts) via a cheap LLM call, and persist them.
* `context` — before each new message, pull the relevant memory into a small,
  **compressed** block for the system prompt.

This is *not* the per-connection conversation history (`app.session`): that's the
LLM's short-term context and dies with the socket. Memory outlives every session.

The public surface the pipeline uses is just two coroutines — `remember` (write
half) and `build_context` (read half) — plus `get_store` for direct access.
"""

from __future__ import annotations

from app.memory.context import build_context
from app.memory.store import MemoryStore, get_store
from app.memory.writer import extract_facts, remember

__all__ = [
    "MemoryStore",
    "get_store",
    "remember",
    "extract_facts",
    "build_context",
]
