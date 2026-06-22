"""Reminder tool — record something to remind the user about (Phase 4).

``set_reminder`` durably stores the reminder text and, if the user gave a time, an
ISO-8601 timestamp. The model is responsible for converting a spoken time ("at
half five", "tomorrow morning") into ISO — it knows the current date/time from the
system prompt; this tool just persists what it's handed.

**Scope note:** firing/delivery is intentionally *not* wired up yet. Speaking a
reminder at its due time needs a scheduler plus a server-initiated push frame on
the WS protocol (the wire contract today is request/response per utterance). That
lands later; for now this captures the intent so nothing is dropped.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.memory.store import get_store
from app.tools.registry import registry


def _normalize_when(when: str | None) -> str | None:
    """Best-effort: keep ``when`` only if it parses as an ISO-8601 datetime.

    The model is asked for ISO; if it sends something else we store ``None`` rather
    than a junk timestamp, so a future scheduler can trust the column.
    """
    if not when or not when.strip():
        return None
    raw = when.strip()
    try:
        # Accept a trailing 'Z' (UTC) which fromisoformat handles only in 3.11+.
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return raw


@registry.register(
    name="set_reminder",
    description=(
        "Record a reminder for the user. Give the reminder text, and if the user "
        "mentioned a time, an ISO-8601 timestamp for when to remind them "
        "(e.g. '2026-06-22T17:30:00'); omit the time if none was given. Use this "
        "when the user says something like 'remind me to ...'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "What to remind the user about.",
            },
            "when": {
                "type": "string",
                "description": (
                    "When to remind, as an ISO-8601 timestamp. Omit if the user "
                    "gave no specific time."
                ),
            },
        },
        "required": ["text"],
    },
)
async def set_reminder(text: str, when: str | None = None) -> str:
    text = (text or "").strip()
    if not text:
        return "Error: a reminder needs something to remind about."
    remind_at = _normalize_when(when)
    reminder_id = await asyncio.to_thread(get_store().add_reminder, text, remind_at)
    if remind_at:
        return f"Reminder #{reminder_id} saved for {remind_at}: {text}"
    return f"Reminder #{reminder_id} saved: {text}"
