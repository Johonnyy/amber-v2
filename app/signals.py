"""Telemetry — the raw material for Amber noticing how she's doing.

Nothing in Amber recorded whether a tool worked, whether a search came back empty,
whether the user cut her off, or whether they had to correct her. Without that, the
maintenance pass has nothing to reflect on and "gets better over time" is a slogan.

Five signals, all cheap:

* ``tool_call`` — name, success, latency. Captured in ``registry.dispatch``, the one
  place every tool already passes through.
* ``empty_result`` — a lookup that found nothing. Distinct from a failure: it means
  the tool worked and the answer wasn't there.
* ``correction`` — the user pushing back ("no, actually..."). Heuristic and
  deliberately conservative; a false positive costs one junk row, and the
  maintenance pass requires several before it concludes anything.
* ``barge_in`` — an interrupted reply, which usually means too long or wrong.
* ``turn`` — modality, sentences spoken, whether memory was drawn on.

**A signal must never cost a turn anything.** Writes go through a bounded queue
drained by one background task, so nothing on the speech path waits on SQLite; a
full queue drops the oldest row rather than applying backpressure, because losing
telemetry is always better than delaying speech. Every failure here is swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

from app.config import get_settings
from app.memory.store import get_store

logger = logging.getLogger(__name__)

KIND_TOOL_CALL = "tool_call"
KIND_EMPTY_RESULT = "empty_result"
KIND_CORRECTION = "correction"
KIND_BARGE_IN = "barge_in"
KIND_TURN = "turn"

# Deep enough to absorb a busy exchange, shallow enough that a stalled drain can't
# grow without bound.
_QUEUE_SIZE = 512
# How long the drain waits to batch rows before committing.
_FLUSH_INTERVAL_S = 2.0
_BATCH_SIZE = 64

_queue: asyncio.Queue | None = None
_dropped = 0


def _get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    return _queue


def record(
    kind: str,
    *,
    name: str | None = None,
    ok: bool | None = None,
    latency_ms: int | None = None,
    detail: str | None = None,
    session_id: str | None = None,
) -> None:
    """Queue one telemetry row. Synchronous, non-blocking, and never raises.

    Deliberately not a coroutine: callers are on the turn path and shouldn't have to
    think about awaiting telemetry, or about what happens when the drain is behind.
    """
    settings = get_settings()
    if not settings.feature_signals:
        return

    row: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "ok": ok,
        "latency_ms": latency_ms,
        # Bounded: a detail field is a hint for a later summary, not a log.
        "detail": detail[:500] if isinstance(detail, str) else detail,
        "session_id": session_id,
    }

    try:
        _get_queue().put_nowait(row)
    except asyncio.QueueFull:
        _drop()
    except RuntimeError:
        # No event loop (e.g. a synchronous test touching a tool directly). Nothing
        # to do; telemetry is not worth constructing a loop for.
        pass


def _drop() -> None:
    global _dropped
    _dropped += 1
    if _dropped % 100 == 1:
        logger.warning("Signal queue full; dropped %d event(s) so far", _dropped)


async def drain_once(*, store=None) -> int:
    """Write everything currently queued. Returns how many rows landed."""
    queue = _get_queue()
    rows: list[dict] = []
    while len(rows) < _BATCH_SIZE:
        try:
            rows.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if not rows:
        return 0
    try:
        return await asyncio.to_thread((store or get_store()).add_turn_events, rows)
    except Exception:  # noqa: BLE001 — telemetry must never surface as a failure
        logger.exception("Failed to write %d signal(s) (non-fatal)", len(rows))
        return 0


async def drain_loop(interval_s: float = _FLUSH_INTERVAL_S) -> None:
    """Batch-write queued signals forever. Started and cancelled by the lifespan."""
    logger.info("Signal writer started")
    try:
        while True:
            await asyncio.sleep(interval_s)
            await drain_once()
    except asyncio.CancelledError:
        # Last flush on the way out, so a clean shutdown doesn't lose the tail.
        with contextlib.suppress(Exception):
            await drain_once()
        raise


# A conservative set: each pattern is something people say when they are correcting
# an assistant and rarely otherwise. Over-matching would teach the maintenance pass
# that Amber is wrong constantly, which is worse than missing some corrections.
_CORRECTION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # The comma is doing real work in both of these. "No, I meant Denver" is a
        # correction; "no problem, thanks" is not. "Actually, make it tomorrow" is;
        # "actually useful, that" is not.
        r"^\s*(no|nope)\s*[,.]\s",
        r"\bthat'?s (not right|wrong|incorrect)\b",
        r"\bi (said|meant)\b",
        r"^\s*actually\s*[,.]\s",
        r"\byou (got that wrong|misheard|misunderstood)\b",
        r"\bnot what i (said|meant|asked)\b",
        r"\bwrong\b.*\b(again|still)\b",
    )
)


def looks_like_a_correction(text: str) -> bool:
    """True if this utterance reads as the user pushing back on the last reply."""
    if not text or len(text) > 400:
        return False
    return any(p.search(text) for p in _CORRECTION_PATTERNS)


def reset_for_tests() -> None:
    """Drop the queue so one test's signals can't leak into another's."""
    global _queue, _dropped
    _queue = None
    _dropped = 0
