"""Memory writer — distil an exchange into durable facts and persist them.

After a turn is spoken, `remember` asks a cheap model to read the single exchange
(what the user said, what Amber replied) and return only the things worth keeping
long-term: stable preferences, identity, ongoing tasks, recurring patterns. It
stores **punchy distilled facts, not raw transcripts** — every fact later costs
tokens on every LLM call, so the bar is "would Amber be worse off forgetting
this?".

Two details matter for keeping the store clean as it grows:

* **The "already known" list is searched, not sampled.** It used to be the N most
  recently stored facts, which stops being useful the moment the store outgrows N —
  the model would happily re-offer a near-duplicate of something it couldn't see.
  Now the store is searched with the exchange itself, so what the model is told it
  already knows is what's actually *relevant* to what was just said.
* **The exchange is logged before extraction, not after.** A failed extraction (or a
  barge-in cancelling this coroutine) used to lose the raw conversation record too,
  because logging came last.

Facts are tiered on the way in: an ordinary distilled fact is ``short`` and earns
``durable`` by proving useful (see `app.maintenance`), while something the extractor
marks as identity-level starts durable. Amber can also commit a fact deliberately
through the `remember_fact` tool, which bypasses this path entirely.

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
from typing import Any

from app.config import Settings, get_settings
from app.memory.store import (
    SOURCE_EXTRACTED,
    TIER_DURABLE,
    TIER_SHORT,
    MemoryStore,
    get_store,
)

logger = logging.getLogger(__name__)

# Categories the extractor may assign. Kept short and concrete — a long taxonomy
# just produces inconsistent labelling. They exist to make the future markdown
# export (one file per category) obvious, and to help the ranker later.
_CATEGORIES = ("identity", "preference", "project", "relationship", "commitment", "pattern")

# A distilled fact is one short sentence. Past this it isn't a fact, it's a summary.
_MAX_FACT_CHARS = 200

_EXTRACT_SYSTEM = """\
You maintain the long-term memory of a personal assistant named Amber about its user.

You are given ONE exchange (the user's message and Amber's reply) plus the facts
Amber already knows that are relevant to it. Extract only NEW information worth
remembering across future conversations: stable preferences, identity or
relationships, ongoing projects, commitments, and recurring patterns.

Ignore: small talk, one-off questions, transient state, anything already known, and
anything about Amber herself.

Write each fact as ONE short, self-contained sentence about the user, in plain
language ("Prefers tea over coffee", "Is learning Spanish", "Has a dog named
Mango"). No dates, no hedging, no "the user" — just the fact.

If the exchange CONTRADICTS a known fact, do not add a parallel fact. Emit the
corrected version and set "replaces" to the exact text of the fact it replaces.

Classify each fact:
- "category": one of identity, preference, project, relationship, commitment, pattern
- "durable": true only for things that stay true for months or longer (who they are,
  where they live, who matters to them, standing preferences). false for anything
  tied to a current project, mood, or season — those earn permanence by coming up
  again.

Respond with ONLY a JSON array of objects, like:
[{"fact": "Has a dog named Mango", "category": "relationship", "durable": true}]
Return [] if nothing is worth keeping. Be strict — most exchanges yield nothing.
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


def _normalize(item: Any) -> dict | None:
    """One parsed entry as ``{content, category, tier, replaces}``, or None if junk.

    Accepts both the object shape the prompt asks for and a bare string, because a
    cheap model occasionally reverts to the older, simpler format and dropping the
    fact entirely would be the worse failure.
    """
    if isinstance(item, str):
        content, category, durable, replaces = item.strip(), None, False, None
    elif isinstance(item, dict):
        content = str(item.get("fact") or item.get("content") or "").strip()
        category = item.get("category")
        durable = bool(item.get("durable"))
        replaces = item.get("replaces")
        replaces = str(replaces).strip() if replaces else None
    else:
        return None

    if not content or content.lower() in ("none", "n/a"):
        return None
    # A rambling "fact" is worse than none: it goes into every future prompt at full
    # token cost and crowds out the punchy ones. Anything this long is a summary the
    # extractor failed to distil, so drop it rather than truncating mid-sentence.
    if len(content) > _MAX_FACT_CHARS:
        return None
    if category not in _CATEGORIES:
        category = None
    return {
        "content": content,
        "category": category,
        "tier": TIER_DURABLE if durable else TIER_SHORT,
        "replaces": replaces,
    }


def _parse_facts(raw: str, limit: int) -> list[dict]:
    """Parse the model's reply into a clean, capped list of fact records.

    Tolerant of the usual model output quirks: code fences, a JSON array of plain
    strings instead of objects, or a newline/bulleted list instead of any JSON.
    """
    text = _FENCE_RE.sub("", raw.strip()).strip()
    items: list[Any] = []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            items = list(parsed)
        elif isinstance(parsed, dict):
            # A lone object, or {"facts": [...]} — both show up occasionally.
            inner = parsed.get("facts")
            items = list(inner) if isinstance(inner, list) else [parsed]
    except (json.JSONDecodeError, ValueError):
        # Fallback: treat each non-empty line as a fact, stripping bullets/quotes.
        for line in text.splitlines():
            line = line.strip().lstrip("-*•").strip().strip('"').strip()
            if line and line not in ("[", "]"):
                items.append(line)

    facts = [f for f in (_normalize(i) for i in items) if f is not None]
    return facts[:limit]


async def extract_facts(
    user_text: str,
    assistant_text: str,
    known: Iterable[str] = (),
    *,
    settings: Settings | None = None,
    runner: Any | None = None,
    conversation_id: str | None = None,
) -> list[dict]:
    """Ask the model for durable facts in this exchange. Never raises for empty input."""
    if not user_text.strip() or not assistant_text.strip():
        return []
    settings = settings or get_settings()
    if runner is None:
        # Imported lazily: the brain (transitively) imports the tools package,
        # which imports memory — a module-level import would close that loop into a
        # circular one. Only needed at call time, so fetch it here.
        from agent_runtime import AgentRunner

        from app.brain import runtime_settings

        runner = AgentRunner(
            model=settings.memory_tier,
            # No tools: this is a single extraction call, and offering tools to it
            # would let a fact-distillation step start doing things.
            broker=None,
            max_tokens=settings.memory_extract_max_tokens,
            settings=runtime_settings(settings),
        )

    # `stream` and join, rather than `run`, because `run` drives the sentence
    # splitter — which is right for speech and wrong for a JSON array.
    raw = "".join(
        [
            chunk
            async for chunk in runner.stream(
                _build_payload(user_text, assistant_text, known),
                system=_EXTRACT_SYSTEM,
                conversation_id=conversation_id,
            )
        ]
    )
    return _parse_facts(raw, settings.memory_max_new_facts)


async def _relevant_known(
    store: MemoryStore, user_text: str, assistant_text: str, limit: int
) -> list[dict]:
    """Facts the model should treat as already known for *this* exchange.

    Search first (what's relevant), then top up with the newest facts so a brand-new
    store — where search finds nothing — still suppresses obvious repeats.
    """
    if limit <= 0:
        return []
    found = await asyncio.to_thread(
        store.search_facts, f"{user_text} {assistant_text}", limit
    )
    if len(found) < limit:
        seen = {row["id"] for row in found}
        recent = await asyncio.to_thread(store.recent_facts, limit - len(found))
        found = found + [r for r in recent if r["id"] not in seen]
    return found[:limit]


async def remember(
    user_text: str,
    assistant_text: str,
    *,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
    conversation_id: str | None = None,
) -> list[str]:
    """Distil and persist memory from one exchange; log the exchange itself.

    Returns the list of *newly stored* facts (empty if memory is off, the exchange
    was empty, or nothing new was found). A fact that merely reinforced one Amber
    already held is not in the list — it strengthened an existing row rather than
    adding one. Honors ``feature_memory`` so a single guard disables both halves.
    """
    settings = settings or get_settings()
    if not settings.feature_memory:
        return []
    store = store or get_store()

    # Log first: the raw exchange is the substrate everything else is derived from,
    # so it must survive an extraction failure or a barge-in cancelling this call.
    await asyncio.to_thread(store.log_exchange, user_text, assistant_text)

    known_rows = await _relevant_known(
        store, user_text, assistant_text, settings.memory_max_facts
    )
    known = [row["content"] for row in known_rows]
    known_by_text = {row["content"].strip().lower(): row for row in known_rows}

    facts = await extract_facts(
        user_text,
        assistant_text,
        known,
        settings=settings,
        conversation_id=conversation_id,
    )

    stored: list[str] = []
    for entry in facts:
        # Normalize again here: `extract_facts` is a seam callers substitute, and a
        # plain string is a legitimate thing for one to hand back.
        fact = _normalize(entry)
        if fact is None:
            continue
        content = fact["content"]

        # A correction supersedes rather than accumulating a contradicting twin.
        target = known_by_text.get((fact.get("replaces") or "").strip().lower())
        if target is not None:
            new_id = await asyncio.to_thread(
                store.supersede_fact,
                target["id"],
                content,
                category=fact["category"],
                tier=fact["tier"],
                confidence=0.75,
                source=SOURCE_EXTRACTED,
            )
            if new_id is not None:
                stored.append(content)
            continue

        # `add_fact` reinforces an existing fact instead of inserting a duplicate,
        # so ask first — "stored" should mean a genuinely new fact.
        is_new = not await asyncio.to_thread(store.fact_exists, content)
        fact_id = await asyncio.to_thread(
            store.add_fact,
            content,
            fact["category"],
            tier=fact["tier"],
            confidence=0.6,
            source=SOURCE_EXTRACTED,
        )
        if fact_id is not None and is_new:
            stored.append(content)

    if stored:
        logger.info("Memory: stored %d new fact(s)", len(stored))
    return stored
