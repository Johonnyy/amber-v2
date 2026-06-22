"""Memory writer — distil an exchange into durable facts and persist them.

After a turn is spoken, `remember` asks a cheap model to read the single exchange
(what the user said, what Amber replied) and return only the things worth keeping
long-term: stable preferences, identity, ongoing tasks, recurring patterns. It
stores **punchy distilled facts, not raw transcripts** — every fact later costs
tokens on every LLM call, so the bar is "would Amber be worse off forgetting
this?".

Known facts are passed to the model so it can skip what's already stored; the
store's unique index is a second, exact-match safety net against duplicates.

This runs **off the latency path** — the pipeline calls it after the audio and
``turn_complete`` are already on the wire — so a slow or failed extraction never
delays speech. It's best-effort by design: callers wrap it so a failure is logged,
not fatal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable

from anthropic import AsyncAnthropic

from app.brain import get_client
from app.config import Settings, get_settings
from app.memory.store import MemoryStore, get_store

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = """\
You maintain the long-term memory of a voice assistant named Amber about its user.

You are given ONE exchange (the user's message and Amber's reply) plus facts that
are already known. Extract only NEW information worth remembering across future
conversations: stable preferences, identity or relationships, ongoing projects or
tasks, commitments, and recurring patterns.

Ignore: small talk, one-off questions, transient state, anything already known,
and anything about Amber itself.

Write each fact as ONE short, self-contained sentence about the user, in plain
language (e.g. "Prefers tea over coffee", "Is learning Spanish", "Has a dog named
Mango"). Do not include dates or hedging.

Respond with ONLY a JSON array of strings. Return [] if nothing is worth keeping.
"""


def _format_known(known: Iterable[str]) -> str:
    facts = [f for f in known if f]
    if not facts:
        return "(none yet)"
    return "\n".join(f"- {f}" for f in facts)


def _build_payload(user_text: str, assistant_text: str, known: Iterable[str]) -> str:
    return (
        f"Already known (do not repeat these):\n{_format_known(known)}\n\n"
        f"Exchange:\nUser: {user_text}\nAmber: {assistant_text}"
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_facts(raw: str, limit: int) -> list[str]:
    """Parse the model's reply into a clean, capped list of fact strings.

    Tolerant of the usual model output quirks: code fences, or a plain
    newline/bulleted list instead of strict JSON.
    """
    text = _FENCE_RE.sub("", raw.strip()).strip()
    facts: list[str] = []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            facts = [str(item).strip() for item in parsed]
    except (json.JSONDecodeError, ValueError):
        # Fallback: treat each non-empty line as a fact, stripping bullets/quotes.
        for line in text.splitlines():
            line = line.strip().lstrip("-*•").strip().strip('"').strip()
            if line and line not in ("[", "]"):
                facts.append(line)

    # Drop empties and obvious non-facts, then cap.
    cleaned = [f for f in facts if f and f.lower() not in ("none", "n/a")]
    return cleaned[:limit]


async def extract_facts(
    user_text: str,
    assistant_text: str,
    known: Iterable[str] = (),
    *,
    settings: Settings | None = None,
    client: AsyncAnthropic | None = None,
) -> list[str]:
    """Ask the model for durable facts in this exchange. Never raises for empty input."""
    if not user_text.strip() or not assistant_text.strip():
        return []
    settings = settings or get_settings()
    client = client or get_client()

    resp = await client.messages.create(
        model=settings.memory_model,
        max_tokens=settings.memory_extract_max_tokens,
        system=_EXTRACT_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": _build_payload(user_text, assistant_text, known),
            }
        ],
    )
    raw = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
    return _parse_facts(raw, settings.memory_max_new_facts)


async def remember(
    user_text: str,
    assistant_text: str,
    *,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Distil and persist memory from one exchange; log the exchange itself.

    Returns the list of newly stored facts (empty if memory is off, the exchange
    was empty, or nothing new was found). Honors ``feature_memory`` so a single
    guard disables both halves of memory.
    """
    settings = settings or get_settings()
    if not settings.feature_memory:
        return []
    store = store or get_store()

    # Hand the model what we already know so it doesn't re-offer it.
    known_rows = await asyncio.to_thread(store.recent_facts, settings.memory_max_facts)
    known = [row["content"] for row in known_rows]

    facts = await extract_facts(
        user_text, assistant_text, known, settings=settings
    )

    stored: list[str] = []
    for fact in facts:
        fact_id = await asyncio.to_thread(store.add_fact, fact)
        if fact_id is not None:
            stored.append(fact)

    # Always keep the raw exchange in the durable log, even if no fact was distilled.
    await asyncio.to_thread(store.log_exchange, user_text, assistant_text)

    if stored:
        logger.info("Memory: stored %d new fact(s)", len(stored))
    return stored
