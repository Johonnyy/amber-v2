"""Context builder — pull the relevant memory into the system prompt.

Before each new user message, this assembles a small, **compressed** block of what
Amber knows (facts + open tasks) and hands it to the pipeline, which appends it to
the persona prompt. Every fact here is paid for in tokens on every LLM call, so the
block is deliberately tight: capped by count *and* by characters, ranked so the most
useful facts survive the cut.

**Retrieval is bounded, not exhaustive.** It used to read every fact in the table and
tokenize all of them in Python on the latency path, scoring by raw keyword-overlap
count with insertion order as the only tiebreak. That degrades exactly as memory
grows — which is the point at which retrieval starts mattering. Now the store's
index supplies a bounded candidate pool and the scorer weighs five signals:

* **relevance** — where the fact ranked in the index search for this utterance,
* **tier** — a durable fact outranks one still proving itself,
* **confidence** — how sure we are it's true,
* **use** — facts that keep earning their place win ties,
* **recency** — freshly learned or freshly used beats long-untouched.

Relevance is still **lexical, not semantic**: no embeddings, no extra network call
on the latency path. The one thing that isn't relevance-gated is a small slice of
the most-used durable facts, always included — identity-level knowledge (who he is,
where he lives, how he likes answers) rarely shares words with the question, so pure
relevance ranking drops precisely the facts that matter most.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.memory.store import TIER_DURABLE, MemoryStore, get_store

# Score weights. These live in code rather than config on purpose: they want tuning
# against real transcripts in a diff someone can review, not in a .env nobody can
# reconstruct later. Each component is normalized to 0..1 before weighting.
_W_RELEVANCE = 1.0   # matched this utterance, and how well
_W_TIER = 0.35       # durable knowledge over provisional
_W_CONFIDENCE = 0.20
_W_USE = 0.25        # has repeatedly earned its tokens
_W_RECENCY = 0.30

_TIER_WEIGHT = {TIER_DURABLE: 1.0, "short": 0.8, "session": 0.6}

# Use counts saturate: the difference between one use and five is meaningful, the
# difference between fifty and sixty is not.
_USE_SATURATION = 10.0
# A fact untouched for this long is worth half as much on the recency axis alone.
_RECENCY_HALF_LIFE_DAYS = 30.0
# How much of the relevance score a fact earns simply by matching at all, with the
# rest allocated by where it placed. Matching is the big signal; placement is a nudge.
_MATCH_FLOOR = 0.8


@dataclass(frozen=True)
class MemoryView:
    """One per-turn read of memory, in every shape its consumers need.

    Built in a single pass so the copy the *model* gets and the copy the *user*
    sees cannot drift:

    * ``block`` — the compressed text appended to the system prompt, or ``None``.
    * ``items`` — the ranked fact strings for the ``memory`` protocol frame.
    * ``facts`` — the same facts as ``{id, content, tier}`` records, for a client
      that wants to render or reference them.
    * ``fact_ids`` — what the pipeline touches after the turn completes, which is
      the only evidence memory has that a fact was worth including.
    """

    block: str | None = None
    items: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    fact_ids: list[int] = field(default_factory=list)


def _age_days(row: dict, now: datetime) -> float:
    """How long since this fact was last used, or learned if it never has been."""
    stamp = row.get("last_used_at") or row.get("created_at")
    if not stamp:
        return 0.0
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400.0)


def _score(row: dict, relevance: float, now: datetime) -> float:
    """Blend the five signals into one comparable number."""
    tier = _TIER_WEIGHT.get(row.get("tier") or "", 0.8)
    confidence = min(1.0, max(0.0, float(row.get("confidence") or 0.0)))
    use = min(
        1.0, math.log1p(float(row.get("use_count") or 0)) / math.log1p(_USE_SATURATION)
    )
    recency = 0.5 ** (_age_days(row, now) / _RECENCY_HALF_LIFE_DAYS)
    return (
        _W_RELEVANCE * relevance
        + _W_TIER * tier
        + _W_CONFIDENCE * confidence
        + _W_USE * use
        + _W_RECENCY * recency
    )


async def rank_facts(
    query: str | None,
    *,
    store: MemoryStore,
    settings: Settings,
) -> list[dict]:
    """The facts worth spending this turn's tokens on, best first.

    Two bounded reads build the candidate pool — an index search for this utterance,
    and the most-used durable facts regardless of it — then everything is scored
    together and cut to the count and character budgets.
    """
    limit = settings.memory_max_facts
    if limit <= 0:
        return []

    matched: list[dict] = []
    if query and query.strip():
        matched = await asyncio.to_thread(
            store.search_facts, query, settings.memory_candidates
        )

    # Relevance by position in the search result, rather than raw bm25: the scores
    # differ in scale between queries and don't exist at all in the LIKE fallback,
    # so position is the one signal both paths agree on.
    #
    # Matching at all is most of the signal (_MATCH_FLOOR); where a fact placed
    # within the matches is a small tilt on top. Spreading position across the full
    # range instead would make the gap between first and second place swamp tier,
    # confidence and usage combined — and with two candidates that gap is enormous
    # and almost meaningless.
    span = max(1, len(matched))
    candidates: dict[int, tuple[dict, float]] = {
        row["id"]: (
            row,
            _MATCH_FLOOR + (1.0 - _MATCH_FLOOR) * (1.0 - index / span),
        )
        for index, row in enumerate(matched)
    }

    always = await asyncio.to_thread(
        store.active_facts,
        tier=TIER_DURABLE,
        limit=settings.memory_always_durable,
        order="used",
    )
    for row in always:
        candidates.setdefault(row["id"], (row, 0.0))

    if not candidates:
        # Nothing matched and nothing durable yet — fall back to the newest facts so
        # a young memory still has something to say rather than a blank slate.
        recent = await asyncio.to_thread(store.recent_facts, limit)
        candidates = {row["id"]: (row, 0.0) for row in recent}

    now = datetime.now(timezone.utc)
    scored = sorted(
        candidates.values(),
        key=lambda pair: (-_score(pair[0], pair[1], now), -pair[0]["id"]),
    )

    chosen: list[dict] = []
    budget = settings.memory_max_chars
    used = 0
    for row, _relevance in scored:
        if len(chosen) >= limit:
            break
        cost = len(row["content"]) + 1
        if budget > 0 and used + cost > budget and chosen:
            # Out of characters, not out of facts. Stop rather than truncating one.
            break
        chosen.append(row)
        used += cost
    return chosen


def _format_block(facts: list[dict], tasks: list[dict]) -> str | None:
    """The prompt block. Ids are included so the model can act on a specific row.

    Without them, correcting or forgetting a fact means a search round trip first;
    with them, ``forget_fact`` is one call. They cost ~3 tokens each.
    """
    lines: list[str] = []
    if facts:
        lines.append(
            "What you remember about your user (ids in brackets; never say them aloud):"
        )
        lines.extend(f"- [#{f['id']}] {f['content']}" for f in facts)
    if tasks:
        if lines:
            lines.append("")
        lines.append("Open tasks you're tracking for them:")
        lines.extend(f"- [#{t['id']}] {t['description']}" for t in tasks)
    if not lines:
        return None
    return "\n".join(lines)


def _format_recap(messages: list[dict]) -> str | None:
    """A short "where you left off" replay of the most recent durable messages.

    ``messages`` arrives oldest-first (as ``store.recent_messages`` returns it), so
    the recap reads in conversation order. Only used to cold-start a fresh session;
    once the live history has turns it already carries recent context.
    """
    if not messages:
        return None
    lines = ["Picking up from your last conversation (oldest to newest):"]
    for m in messages:
        speaker = "You" if m["role"] == "assistant" else "They"
        lines.append(f"- {speaker}: {m['content']}")
    return "\n".join(lines)


def _format_notes(reflections: list[dict]) -> str | None:
    """The maintenance pass's own observations, when they're switched on.

    Bulleted with "•" rather than "-" so a fact-counting assertion (or a reader)
    can still tell the two sections apart at a glance.
    """
    if not reflections:
        return None
    lines = ["Patterns you've noticed in these conversations:"]
    lines.extend(f"• {r['note']}" for r in reflections)
    return "\n".join(lines)


async def build_notes_block(
    *,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
) -> str | None:
    """What Amber has noticed about how conversations go, for the system prompt.

    Gated by ``AMBER_FEATURE_SELF_NOTES``, **off by default**. Reflections are worth
    reading long before they're worth acting on automatically, and this flag is the
    line between "Amber notices patterns" and "Amber edits her own instructions".
    """
    settings = settings or get_settings()
    if not (settings.feature_memory and settings.feature_self_notes):
        return None
    store = store or get_store()
    reflections = await asyncio.to_thread(
        store.recent_reflections, settings.maintenance_max_reflections
    )
    return _format_notes(reflections)


async def build_memory_view(
    query: str | None = None,
    *,
    include_recap: bool = False,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
) -> MemoryView:
    """Build every half of the per-turn memory read in one store pass.

    ``include_recap`` is meant for a cold session start (empty live history); a
    session with its own history already carries recent context, so the caller
    leaves it off to avoid replaying turns the model already has. The recap is
    prompt-only — it never appears in ``items``.

    Returns an empty view when memory is disabled.
    """
    settings = settings or get_settings()
    if not settings.feature_memory:
        return MemoryView()
    store = store or get_store()

    facts = await rank_facts(query, store=store, settings=settings)
    tasks = await asyncio.to_thread(store.open_tasks, settings.memory_max_tasks)
    block = _format_block(facts, tasks)

    if include_recap and settings.recent_recap_messages > 0:
        recent = await asyncio.to_thread(
            store.recent_messages, settings.recent_recap_messages
        )
        recap = _format_recap(recent)
        if recap:
            block = f"{block}\n\n{recap}" if block else recap

    return MemoryView(
        block=block,
        items=[f["content"] for f in facts],
        facts=[
            {"id": f["id"], "content": f["content"], "tier": f["tier"]} for f in facts
        ],
        fact_ids=[f["id"] for f in facts],
    )
