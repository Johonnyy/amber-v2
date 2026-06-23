"""Ambient runtime context — the "right now" Amber gets on every turn.

The persona prompt is static and the memory block is *durable* knowledge; neither
tells Amber *when* it is. Without that, "what's today?", "this morning", "in two
hours", or "is it the weekend?" can't be answered. This builds a tiny, always-fresh
block — the current date and time — that the pipeline injects into the system
prompt every turn, **independent of the memory feature flag** (knowing the date is
fundamental, not optional).

Kept to a single line on purpose: it's paid for in tokens on every LLM call. The
clock is read in `settings.timezone` (an IANA name like ``America/New_York``); an
unknown/unavailable zone falls back to UTC rather than failing a turn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _resolve_tz(name: str):
    """The configured zone, or UTC if it can't be loaded.

    On systems without the IANA tz database (e.g. a bare Windows box) ``ZoneInfo``
    raises; the ``tzdata`` package supplies it. Either way a bad/missing zone must
    never break a turn, so we degrade to UTC and log once.
    """
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning("Unknown timezone %r; falling back to UTC", name)
        return timezone.utc


def _format(now: datetime) -> str:
    """A spoken-friendly stamp: 'Tuesday, June 23, 2026 at 3:45 PM EDT'.

    Built by hand rather than ``strftime`` because the leading-zero-stripping
    directives (``%-d``/``%-I``) aren't portable to Windows.
    """
    hour12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    stamp = (
        f"{now:%A}, {now:%B} {now.day}, {now.year} "
        f"at {hour12}:{now.minute:02d} {ampm}"
    )
    tzname = now.tzname()
    return f"{stamp} {tzname}" if tzname else stamp


def build_runtime_context(
    *, settings: Settings | None = None, now: datetime | None = None
) -> str:
    """The ambient context block injected into the system prompt every turn.

    ``now`` is overridable for tests; in production it's the wall clock in the
    configured timezone. Always returns a non-empty string — date/time is never
    suppressed.
    """
    settings = settings or get_settings()
    if now is None:
        now = datetime.now(_resolve_tz(settings.timezone))
    return (
        f"Right now it's {_format(now)}. "
        "Use this for anything about the date, day of week, or time."
    )
