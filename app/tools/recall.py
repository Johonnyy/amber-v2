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
        "Look up what was said in the user's recent conversations. Use ONLY when "
        "the user refers back to something from an earlier talk that isn't in the "
        "current conversation — e.g. 'what did I ask you about earlier', 'that "
        "thing from yesterday'. Returns the most recent logged messages, oldest "
        "first."
    ),
    input_schema={"type": "object", "properties": {}},
    available=lambda: get_settings().feature_memory,
)
async def recall_recent() -> str:
    limit = get_settings().recall_messages
    messages = await asyncio.to_thread(get_store().recent_messages, limit)
    if not messages:
        return "No earlier conversations on record."
    lines = [
        f"{'You' if m['role'] == 'assistant' else 'They'}: {m['content']}"
        for m in messages
    ]
    return "Recent conversation (oldest to newest):\n" + "\n".join(lines)
