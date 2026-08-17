"""Firing reminders — the half that was always missing.

`set_reminder` has been able to record a reminder since Phase 4, `list_reminders` to list
one and `complete_reminder` to tick it off. What none of them could do was *arrive*.
`MemoryStore.due_reminders` was written, indexed, and called by nothing but a test,
because a due reminder had nowhere to go: every frame in the protocol was a reply to
something the client had just done. `app.push` is that missing direction, and this is the
scheduler that feeds it.

Deliberately its own loop rather than a step in `app.maintenance`, which would have come
with failure isolation and a report line for free. The cadence is wrong by three orders
of magnitude: maintenance runs every six hours behind a five-minute startup delay, so a
reminder set for 17:30 would land at some arbitrary point before midnight. Tightening
that interval to suit reminders would multiply the cost of an LLM pass that has no reason
to run more often.

**Due-ness is decided in Python, not SQL, and that is not an accident.** `remind_at` was
stored verbatim for most of this project's life — the tool asks the model for local time
"with no offset", so historical rows are naive while `_now()` has always been UTC. A
lexical `remind_at <= now` therefore mis-fired every one of them by the length of the
user's offset: six hours early in Denver. New rows are normalised on write
(`app.tools.reminders.parse_when`), but old ones still exist, so this parses each
candidate and compares instants, reading a naive value as ``AMBER_TIMEZONE`` — the same
interpretation the tool description asked the model for. One person's reminders is a
small enough set that correctness is worth more than the index.

**Marked fired only after the push is safely enqueued.** The ordering matters in exactly
one direction: a crash between the two re-announces a reminder, which is a mild
annoyance, where the reverse silently loses it, which is the failure this whole feature
exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app import protocol, push
from app.config import Settings, get_settings
from app.memory.store import get_store
from app.runtime_context import _resolve_tz
from app.tools.reminders import parse_when

logger = logging.getLogger(__name__)


def enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.feature_reminder_delivery
        and settings.feature_memory
        and push.enabled(settings)
    )


def is_due(remind_at: str | None, now: datetime, *, tz=None) -> bool:
    """Whether a stored reminder time has arrived. Unparseable times never fire.

    Refusing to guess is the safe direction: a reminder that never fires is visible in
    `list_reminders` and can be asked about, where one that fires at the wrong moment
    teaches the user not to trust any of them.
    """
    if not remind_at:
        return False
    try:
        return parse_when(remind_at, tz=tz) <= now
    except (ValueError, TypeError):
        logger.warning("Unparseable reminder time %r; not firing", remind_at)
        return False


async def fire_due(settings: Settings | None = None, *, now: datetime | None = None) -> int:
    """Hand every due reminder to the push layer. Returns how many fired.

    ``now`` is injectable for tests. Never raises.
    """
    settings = settings or get_settings()
    if not enabled(settings):
        return 0

    tz = _resolve_tz(settings.timezone)
    now = now or datetime.now(tz)
    store = get_store()

    try:
        candidates = await asyncio.to_thread(store.undelivered_reminders)
    except Exception:  # noqa: BLE001
        logger.exception("Could not read reminders (non-fatal)")
        return 0

    fired = 0
    for row in candidates:
        if not is_due(row.get("remind_at"), now, tz=tz):
            continue
        push_id = await push.enqueue(
            protocol.PUSH_REMINDER,
            row["text"],
            ref={"reminder_id": row["id"]},
        )
        if push_id is None:
            # Couldn't record it — leave the reminder unfired so the next pass retries
            # rather than marking a delivery that will never happen.
            logger.warning("Reminder #%s could not be enqueued; will retry", row["id"])
            continue
        try:
            await asyncio.to_thread(store.mark_reminder_fired, row["id"])
        except Exception:  # noqa: BLE001
            logger.exception("Reminder #%s fired but could not be marked", row["id"])
        fired += 1
        logger.info("Reminder #%s fired: %s", row["id"], row["text"][:60])

    return fired


async def reminder_loop(settings: Settings | None = None) -> None:
    """Check for due reminders forever. Started and cancelled by the lifespan."""
    settings = settings or get_settings()
    interval = max(5.0, settings.reminder_interval_s)
    logger.info(
        "Reminder scheduler started (first check in %.0fs, then every %.0fs)",
        settings.reminder_startup_delay_s,
        interval,
    )
    await asyncio.sleep(settings.reminder_startup_delay_s)
    while True:
        try:
            await fire_due(settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any single failure
            logger.exception("Reminder pass failed (non-fatal)")
        await asyncio.sleep(interval)
