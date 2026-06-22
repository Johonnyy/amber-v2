"""Context builder — pull the relevant memory into the system prompt.

Before each new user message, this assembles a small, **compressed** block of what
Amber knows (facts + any open tasks) and hands it to the pipeline, which appends it
to the persona prompt. Every fact here is paid for in tokens on the LLM call, so
the block is deliberately tight: a hard cap on facts, ranked so the most relevant
ones survive the cut.

Relevance is **lexical, not semantic** — facts are scored by word overlap with the
incoming message, with recency as the tiebreak (and the default order when there's
no query). This is intentionally simple: no embeddings, no extra network call on
the latency path. It's honest about being keyword-based; richer retrieval can slot
in behind the same ``build_context`` signature later without touching callers.
"""

from __future__ import annotations

import asyncio
import re

from app.config import Settings, get_settings
from app.memory.store import MemoryStore, get_store

_WORD_RE = re.compile(r"[a-z0-9']+")
# Common words carry no signal for overlap scoring; ignore them so a shared "the"
# doesn't make every fact look relevant.
_STOPWORDS = frozenset(
    """a an and are as at be but by for from had has have i if in into is it its
    me my of on or our so that the their them they this to was we what when where
    which who will with you your""".split()
)


def _keywords(text: str) -> set[str]:
    return {
        w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1
    }


def _rank_facts(facts: list[dict], query: str | None, limit: int) -> list[dict]:
    """Return up to ``limit`` facts, most relevant first.

    ``facts`` arrives newest-first. With no query we just take the newest ``limit``.
    With a query we score by keyword overlap and keep input order (newest) as the
    stable tiebreak, so equally-relevant facts fall back to recency.
    """
    if limit <= 0:
        return []
    if not query or not query.strip():
        return facts[:limit]

    q = _keywords(query)
    if not q:
        return facts[:limit]

    scored = [(len(q & _keywords(f["content"])), idx, f) for idx, f in enumerate(facts)]
    # High overlap first; lower original index (newer) breaks ties.
    scored.sort(key=lambda t: (-t[0], t[1]))

    ranked = [f for score, _, f in scored if score > 0][:limit]
    if ranked:
        return ranked
    # Nothing overlapped — fall back to the most recent facts so Amber still has
    # baseline context rather than a blank slate.
    return facts[:limit]


def _format_block(facts: list[dict], tasks: list[dict]) -> str | None:
    lines: list[str] = []
    if facts:
        lines.append(
            "What you remember about your user (durable facts; may be incomplete):"
        )
        lines.extend(f"- {f['content']}" for f in facts)
    if tasks:
        if lines:
            lines.append("")
        lines.append("Open tasks you're tracking for them:")
        lines.extend(f"- {t['description']}" for t in tasks)
    if not lines:
        return None
    return "\n".join(lines)


async def build_context(
    query: str | None = None,
    *,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Build the compressed memory block for the system prompt, or ``None``.

    ``query`` is the incoming user message, used to rank facts by relevance.
    Returns ``None`` when memory is off or there's nothing to inject, so the caller
    can fall back to the bare persona prompt.
    """
    settings = settings or get_settings()
    if not settings.feature_memory:
        return None
    store = store or get_store()

    facts = await asyncio.to_thread(store.all_facts)
    tasks = await asyncio.to_thread(store.open_tasks)
    ranked = _rank_facts(facts, query, settings.memory_max_facts)
    return _format_block(ranked, tasks)
