"""Putting a human back in the self-improvement loop.

signals → maintenance → reflections has been a closed circuit. Every tool call has been
timed since telemetry landed, the maintenance pass has written one to three self-review
notes every cycle, and none of it has ever been shown to anyone: `event_summary` fed a
single LLM prompt, `amber://memory/reflections` needed an MCP key, and
`reflections.dismissed` had no writer at all. Amber has been observing herself and
reporting to nobody.

Three topics, one frame trio, following `app.memory_control` exactly — a browse, a
response, and a couple of verbs on what came back:

* **tools** — per-tool success rate and latency percentiles. A degraded tool is
  currently something you notice as a vibe; this is the grid that names it.
* **reflections** — what she noticed, with **promote** and **dismiss**. Promotion is
  the interesting one, and it is what makes `AMBER_FEATURE_SELF_NOTES` safe to leave
  off: you get the value of self-observation without the model editing its own
  instructions, which is the exact line that flag was drawn to protect. A promoted note
  becomes an ordinary durable fact, subject to the same curation as any other.
* **evals** — turns that went wrong, saved as regression cases. CLAUDE.md has asked for
  these since the beginning and none has ever been written, because the moment you
  would write one is the moment something goes wrong and there was no button there.

Everything here is read-mostly and off the turn path, so a failure costs a panel and
never a reply — the posture `app.status` takes, for the same reason.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app import protocol
from app.config import Settings, get_settings
from app.memory.store import (
    SOURCE_EXPLICIT,
    TIER_DURABLE,
    get_store,
)

logger = logging.getLogger(__name__)

TOPICS = (protocol.REVIEW_TOOLS, protocol.REVIEW_REFLECTIONS, protocol.REVIEW_EVALS)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).feature_review)


async def handle_query(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Answer one `review_query`. Never raises into the socket."""
    topic = payload.get("topic")
    if topic not in TOPICS:
        logger.debug("Ignoring review_query for unknown topic %r", topic)
        topic = protocol.REVIEW_TOOLS
    if topic == protocol.REVIEW_EVALS and not settings.feature_evals:
        # `status.features` already reports `evals: false`, so answering with an empty
        # list rather than the tools board keeps the frame's topic matching what was
        # asked — a client switching tabs gets an empty panel, not someone else's data.
        return protocol.review(topic, [])

    limit = payload.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = DEFAULT_LIMIT
    limit = min(limit, MAX_LIMIT)

    return await _browse(topic, limit, payload.get("since"), settings)


async def handle_action(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Apply one `review_action` and answer with the topic as it now stands.

    Always answers, including on refusal — a panel that clicked something needs to know
    it did not take, and silence is indistinguishable from a dropped socket.
    """
    topic = payload.get("topic")
    action = payload.get("action")
    item_id = payload.get("id")

    ok = False
    detail: str | None = None

    if topic not in TOPICS or not isinstance(item_id, int):
        logger.debug("Ignoring malformed review_action: %r", payload)
        return await _browse(
            topic if topic in TOPICS else protocol.REVIEW_TOOLS,
            DEFAULT_LIMIT,
            None,
            settings,
            ack={"action": action, "id": item_id, "ok": False},
        )

    store = get_store()

    if topic == protocol.REVIEW_REFLECTIONS:
        if action == "promote":
            # The note becomes an ordinary durable fact — `source='explicit'` because a
            # person chose to keep it, which is the strongest evidence there is, and the
            # same provenance a "remember this" gets. Amber did not promote herself.
            row = await asyncio.to_thread(store.get_reflection, item_id)
            note = (row or {}).get("note")
            if note:
                fact_id = await asyncio.to_thread(
                    store.add_fact,
                    note,
                    category="pattern",
                    tier=TIER_DURABLE,
                    confidence=0.9,
                    source=SOURCE_EXPLICIT,
                )
                ok = fact_id is not None
                detail = note
                # Promoting settles it: leaving it in the list would invite promoting
                # the same observation twice into two near-identical facts.
                if ok:
                    await asyncio.to_thread(store.dismiss_reflection, item_id)
        elif action == "dismiss":
            ok = await asyncio.to_thread(store.dismiss_reflection, item_id)

    elif topic == protocol.REVIEW_EVALS and settings.feature_evals:
        if action == "archive":
            ok = await asyncio.to_thread(store.archive_eval_case, item_id)

    logger.info("Review %s %s #%s: %s", topic, action, item_id, "ok" if ok else "refused")
    return await _browse(
        topic,
        DEFAULT_LIMIT,
        None,
        settings,
        ack={"action": action, "id": item_id, "ok": ok, **({"detail": detail} if detail else {})},
    )


async def capture_eval(payload: dict[str, Any]) -> int | None:
    """Save a turn as a regression case. Returns the new id, or ``None``.

    The payload carries the whole case rather than a pointer into `conversations` —
    that table has no session id and pairs user with assistant positionally, so a
    reference into it could quietly come to mean a different turn. The client has the
    utterance, the reply and every tool call in hand already.
    """
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return None

    def _str(key: str) -> str | None:
        value = payload.get(key)
        return value.strip()[:2000] if isinstance(value, str) and value.strip() else None

    return await asyncio.to_thread(
        get_store().add_eval_case,
        query.strip()[:2000],
        expect_tool=_str("expect_tool"),
        got_tool=_str("got_tool"),
        note=_str("note"),
        reply=_str("reply"),
    )


async def _browse(
    topic: str,
    limit: int,
    since: Any,
    settings: Settings,
    *,
    ack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = get_store()

    if topic == protocol.REVIEW_TOOLS:
        days = settings.review_default_days
        window = _since(since, days)
        items = await asyncio.to_thread(store.tool_reliability, window, limit)
        return protocol.review(topic, items, ack=ack, since=window)

    if topic == protocol.REVIEW_REFLECTIONS:
        items = await asyncio.to_thread(store.recent_reflections, limit)
        return protocol.review(topic, items, ack=ack)

    items = await asyncio.to_thread(store.eval_cases, limit)
    return protocol.review(topic, items, ack=ack)


def _since(value: Any, default_days: float) -> str:
    """A window start, from the client's request or the configured default."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0.0, default_days))
    return cutoff.isoformat(timespec="seconds")
