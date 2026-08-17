"""Reminder tool — record something to remind the user about (Phase 4).

``set_reminder`` durably stores the reminder text and, if the user gave a time, an
ISO-8601 timestamp. The model is responsible for converting a spoken time ("at
half five", "tomorrow morning") into ISO — it knows the current date/time from the
system prompt; this tool just persists what it's handed.

Reminders **fire** now (`app.reminders` + `app.push`), and that turned an ambiguity
that had never mattered into a bug. This tool asks the model for local time "with no
offset" and used to store exactly that string, while every other timestamp in the
database is UTC — so a lexical comparison against "now" mis-fired every reminder by the
length of the user's offset, six hours early for a user in Denver. A trailing ``Z`` was
also accepted and stored intact, so the column held two incompatible conventions at once.

`_normalize_when` settles it at the point of writing: whatever shape the model produced,
what lands in the row is an offset-aware instant. A naive value is interpreted in
``AMBER_TIMEZONE`` — which is what the model was told to use, so this reads the
instruction rather than guessing. Rows written before this still exist, and
`app.reminders` handles them; see `MemoryStore.undelivered_reminders`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.config import get_settings
from app.memory.store import get_store
from app.runtime_context import _resolve_tz
from app.tools.registry import registry


class _BadTime(ValueError):
    """``when`` was given but isn't a timestamp this tool can store."""


def parse_when(raw: str, *, tz=None) -> datetime:
    """One reminder time as an aware datetime. Raises `ValueError` if it won't parse.

    A naive value means local time in ``AMBER_TIMEZONE``, because that is precisely what
    the tool description asks the model for. Shared with `app.reminders`, so the reading
    that goes into the database and the one that decides a reminder is due can't drift.
    """
    parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or _resolve_tz(get_settings().timezone))
    return parsed


def format_when(raw: str | None, *, tz=None) -> str:
    """A stored reminder time rendered back in the user's own zone.

    The column holds UTC now, and telling someone their 5:30pm reminder is set for
    "23:30:00+00:00" would be technically true and useless. Falls back to the raw string
    if it won't parse, since a legacy row is still worth showing.
    """
    if not raw:
        return ""
    zone = tz or _resolve_tz(get_settings().timezone)
    try:
        local = parse_when(raw, tz=zone).astimezone(zone)
    except ValueError:
        return raw
    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local:%A}, {local:%B} {local.day} at {hour12}:{local.minute:02d} {ampm}"


def _normalize_when(when: str | None) -> str | None:
    """``when`` as a storable, offset-aware ISO-8601 string, or ``None`` if none given.

    Raises `_BadTime` when a time *was* supplied but doesn't parse. It used to
    return ``None`` in that case, which meant the tool reported success and the
    reminder was quietly saved with no time at all — the model would tell the user
    "I'll remind you at five" having stored nothing of the sort.
    """
    if not when or not when.strip():
        return None
    raw = when.strip()
    try:
        parsed = parse_when(raw)
    except ValueError as exc:
        raise _BadTime(raw) from exc
    return parsed.isoformat(timespec="seconds")


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
        # Echoed in the user's own zone, not the UTC that was stored — the model reads
        # this back to them out loud.
        return f"Reminder #{reminder_id} saved for {format_when(remind_at)}: {text}"
    return f"Reminder #{reminder_id} saved (no time given): {text}"
