"""Per-connection conversation history (Phase 2).

History is maintained **per WebSocket connection, in memory** — it lives only as
long as the socket. This is the short-term "what we've been talking about" buffer
that's replayed to the LLM on every turn. It is deliberately *not* the same thing
as persistent memory (Phase 3, SQLite): cross-session knowledge belongs there, not
here. Don't conflate the two.

The message shape matches the Anthropic Messages API directly, so a ``Conversation``
can be handed straight to `app.brain.think`. Consecutive same-role turns are fine —
the API combines them — which means an interrupted turn that saved no assistant
text (so two user turns land in a row) is harmless.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Conversation:
    """In-memory history for one connection."""

    messages: list[dict] = field(default_factory=list)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
