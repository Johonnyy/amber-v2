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


class _BadTime(ValueError):
    """``when`` was given but isn't a timestamp this tool can store."""


def _normalize_when(when: str | None) -> str | None:
    """``when`` as a storable ISO-8601 string, or ``None`` if none was given.

    Raises `_BadTime` when a time *was* supplied but doesn't parse. It used to
    return ``None`` in that case, which meant the tool reported success and the
    reminder was quietly saved with no time at all — the model would tell the user
    "I'll remind you at five" having stored nothing of the sort.
    """
    if not when or not when.strip():
        return None
    raw = when.strip()
    try:
        # Accept a trailing 'Z' (UTC) which fromisoformat handles only in 3.11+.
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _BadTime(raw) from exc
    return raw


@registry.register(
    name="set_reminder",
    description=(
        "Record a reminder for the user. Use it when they say something like "
        "'remind me to...'. Give the reminder text, and — if they mentioned a time "
        "— an ISO-8601 timestamp like '2026-06-22T17:30:00'. Work relative times "
        "('tomorrow at five', 'in an hour') out from the current date and time in "
        "your context, and give it in their local time with no offset. Omit the "
        "time entirely if they didn't give one; don't invent one. For something "
        "with no time at all, add_task is usually the better fit."
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
                    "When to remind, as an ISO-8601 timestamp resolved against the "
                    "current date/time in your context. Omit if no time was given."
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

    try:
        remind_at = _normalize_when(when)
    except _BadTime as bad:
        return (
            f"Error: '{bad}' isn't a timestamp I can store. Use ISO-8601 like "
            "'2026-06-22T17:30:00', worked out from the current date and time in "
            "your context — or leave the time out entirely."
        )

    reminder_id = await asyncio.to_thread(get_store().add_reminder, text, remind_at)
    if remind_at:
        return f"Reminder #{reminder_id} saved for {remind_at}: {text}"
    return f"Reminder #{reminder_id} saved (no time given): {text}"
