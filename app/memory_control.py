"""Letting a person curate Amber's memory, not just the model.

Memory had two ways in and neither belonged to the user. The writer captures what was
merely *said*, automatically, after every turn. The tools capture what was *meant* —
but only when Amber decides to call one, which means "no, forget that" works and
clicking a bin icon does not, because there was no frame that could carry the click.

This is that half. It is deliberately built on the *same* store functions the tools
call — `forget_fact`, `supersede_fact`, `search_facts` — rather than a parallel path,
which is the ecosystem's standing rule about UI and agent reaching the same value:
a fact Amber forgot when asked out loud and one forgotten with a button are the same
row, in the same state, for the same reason.

**Deletion stays soft.** `store.forget_fact` flips a status and keeps the row, which
is what makes an undo honest rather than a lie about a thing that is already gone —
and what lets `restore` exist at all. A mis-click costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import protocol
from app.config import Settings
from app.memory.context import wire_fact
from app.memory.store import STATUS_ACTIVE, get_store

logger = logging.getLogger(__name__)

#: How many facts a browse returns at once. Generous — this is a panel someone is
#: scrolling, not a prompt someone is paying for on every turn.
DEFAULT_BROWSE_LIMIT = 50
MAX_BROWSE_LIMIT = 200

ACTION_FORGET = "forget"
ACTION_RESTORE = "restore"
ACTION_CORRECT = "correct"
ACTIONS = (ACTION_FORGET, ACTION_RESTORE, ACTION_CORRECT)


def enabled(settings: Settings) -> bool:
    return bool(settings.feature_memory and settings.feature_memory_control)


async def handle_action(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Apply one `memory_action` and answer with the fact set as it now stands.

    Always returns a frame, including for a refusal — a panel that clicked something
    needs to know it did not take, and silence is indistinguishable from a dropped
    socket.
    """
    action = payload.get("action")
    fact_id = payload.get("id")

    if action not in ACTIONS or not isinstance(fact_id, int):
        logger.debug("Ignoring malformed memory_action: %r", payload)
        return await _browse(None, DEFAULT_BROWSE_LIMIT, ack=_ack(action, fact_id, False))

    store = get_store()
    ok = False
    content: str | None = None

    if action == ACTION_FORGET:
        row = await asyncio.to_thread(store.forget_fact, fact_id)
        ok = row is not None
        content = row["content"] if row else None
    elif action == ACTION_RESTORE:
        # The other half of a soft delete. Without it "forget" is indistinguishable
        # from a hard one to anyone using the UI, and an undo affordance would be
        # promising something it could not do.
        ok = await asyncio.to_thread(
            store.update_fact, fact_id, status=STATUS_ACTIVE
        )
    elif action == ACTION_CORRECT:
        text = payload.get("content")
        if isinstance(text, str) and text.strip():
            # Supersede rather than edit: the old row stays, marked, pointing at its
            # replacement. A correction is a fact about the facts, and overwriting
            # would throw away the one thing that makes it auditable.
            new_id = await asyncio.to_thread(
                store.supersede_fact, fact_id, text.strip()
            )
            ok = new_id is not None
            content = text.strip()

    logger.info("Memory %s #%s: %s", action, fact_id, "ok" if ok else "refused")
    return await _browse(
        None, DEFAULT_BROWSE_LIMIT, ack=_ack(action, fact_id, ok, content)
    )


async def handle_lineage(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """One fact's revision history, oldest first.

    `supersede_fact` has written `superseded_by` on every correction since tiering
    landed, and nothing has ever read it — so "no, I moved to Denver" built an audit
    trail that no surface could show. This is that chain: *Lives in Boston → Lives in
    Denver*, with when each replacement happened.

    Rides the existing `memory` frame under a new `scope`, which the frame has always
    carried, so a client that doesn't know the scope simply sees a fact list.
    """
    fact_id = payload.get("id")
    if not isinstance(fact_id, int):
        return protocol.memory([], [], scope="lineage")

    rows = await asyncio.to_thread(get_store().lineage, fact_id)
    facts = [wire_fact(row) for row in rows]
    return protocol.memory(
        [f["content"] for f in facts], facts, scope="lineage", total=len(facts)
    )


async def handle_archive(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """What Amber has stopped believing — forgotten and superseded together.

    From a person's point of view that is one list. What it cannot say is *why* a fact
    left: decay, an explicit "forget that" and a consolidation merge all write the same
    row, and telling them apart would need a column that does not exist.
    """
    limit = payload.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = DEFAULT_BROWSE_LIMIT
    store = get_store()
    rows = await asyncio.to_thread(
        store.forgotten_facts, min(limit, MAX_BROWSE_LIMIT)
    )
    facts = [wire_fact(row) for row in rows]
    # The real archive size, not the page size — `total` means "what this is a slice
    # of", and answering it with `len(facts)` would tell a truncated list it was whole.
    def _archive_total() -> int:
        return store.fact_count(status="forgotten") + store.fact_count(
            status="superseded"
        )

    total = await asyncio.to_thread(_archive_total)
    return protocol.memory(
        [f["content"] for f in facts], facts, scope="archive", total=total
    )


async def handle_query(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Answer a `memory_query`: everything Amber knows, or a search of it.

    Distinct from the per-turn `memory` frame, which is only what *this* turn drew
    on. A panel needs both — what is being used right now, and what exists — and
    conflating them would mean a memory could only be deleted on a turn that happened
    to retrieve it.
    """
    # Two extra scopes ride this same frame: a fact's revision history, and the
    # archive of what she no longer believes. Both read columns that have always been
    # written and never read.
    scope = payload.get("scope")
    if scope == "lineage":
        return await handle_lineage(payload, settings)
    if scope == "archive":
        return await handle_archive(payload, settings)

    raw = payload.get("q")
    query = raw.strip() if isinstance(raw, str) and raw.strip() else None
    limit = payload.get("limit")
    if not isinstance(limit, int) or limit <= 0:
        limit = DEFAULT_BROWSE_LIMIT
    return await _browse(query, min(limit, MAX_BROWSE_LIMIT))


async def _browse(
    query: str | None, limit: int, *, ack: dict[str, Any] | None = None
) -> dict[str, Any]:
    store = get_store()
    if query:
        rows = await asyncio.to_thread(store.search_facts, query, limit)
    else:
        rows = await asyncio.to_thread(store.active_facts, limit=limit, order="recent")
    total = await asyncio.to_thread(store.fact_count)
    facts = [wire_fact(row) for row in rows]
    return protocol.memory(
        [f["content"] for f in facts],
        facts,
        scope="browse",
        total=total,
        ack=ack,
    )


def _ack(
    action: Any, fact_id: Any, ok: bool, content: str | None = None
) -> dict[str, Any]:
    frame: dict[str, Any] = {"action": action, "id": fact_id, "ok": ok}
    if content:
        frame["content"] = content
    return frame
