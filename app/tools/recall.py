"""Recall tool — reach into the user's recent durable conversations on demand.

Within a session the live history (see ``app.session.Conversation``) already gives
the model recent context, and a *cold* session start replays the last few durable
messages once (the recap in ``app.memory.context``). The gap is a later turn of a
new/reconnected session: the live history only holds *this* session's turns, so a
follow-up about something said in an earlier conversation can't be answered.

This tool closes that gap without bloating every prompt: it's only offered when
memory is on, and it costs tokens *only* on the turns where the model decides the
user is referring back to an earlier talk and calls it. It reads from the same
``conversations`` table the recap uses, via ``store.recent_messages``.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.memory.store import get_store
from app.tools.registry import registry


@registry.register(
    name="recall_recent",
    description=(
        "Look up what was said in earlier conversations with the user. Use ONLY "
        "when they refer back to something that isn't in the conversation in front "
        "of you — 'what did I ask you about earlier', 'that thing from yesterday'. "
        "Pass a topic to search for it; leave it out to replay the most recent "
        "messages. Returns messages oldest first. For facts about the user rather "
        "than things that were said, use search_memory instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Optional. What the user is referring back to, e.g. 'the "
                    "restaurant' — omit to replay the most recent messages."
                ),
            }
        },
    },
    available=lambda: get_settings().feature_memory,
    read_only=True,
)
async def recall_recent(query: str | None = None) -> str:
    """Search the durable log, or replay the tail of it when given nothing to find."""
    limit = get_settings().recall_messages
    store = get_store()
    query = (query or "").strip()

    if query:
        messages = await asyncio.to_thread(store.search_messages, query, limit)
        if not messages:
            return (
                f"Nothing in earlier conversations mentions '{query}'. Say you "
                "don't remember it rather than guessing."
            )
        header = f"Earlier conversation about '{query}' (oldest to newest):"
    else:
        messages = await asyncio.to_thread(store.recent_messages, limit)
        if not messages:
            return "No earlier conversations on record."
        header = "Recent conversation (oldest to newest):"

    lines = [
        f"{'You' if m['role'] == 'assistant' else 'They'}: {m['content']}"
        for m in messages
    ]
    return header + "\n" + "\n".join(lines)
