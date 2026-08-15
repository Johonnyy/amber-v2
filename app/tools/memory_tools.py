"""Memory tools — how Amber curates what she remembers.

Until now memory happened *to* Amber: a separate extraction pass wrote facts after
each turn, and she had no way to search, commit, correct, or drop one. If the user
said "no, I moved to Denver last year", the best she could do was answer around it
while the old fact stayed in every future prompt. (A peer agent over MCP could
search her facts; she couldn't.)

These four tools close that. They deliberately mirror the two-step shape the task
tools already use — `search_memory` surfaces ids, then `correct_fact` / `forget_fact`
act on one — so she never has to guess an id. Ids also arrive free in the memory
block each turn, so the common case is a single call.

**These do not replace the automatic writer.** Ordinary details are still distilled
in the background, off the latency path; that's what keeps memory working without
Amber spending a tool call on every passing remark. The split is by *intent*:

* the writer captures what was merely *said* — provisional, ``tier='short'``,
  earning permanence by proving useful;
* these tools capture what was *meant* — an explicit "remember this", or a
  correction. That lands ``tier='durable'`` with high confidence and ``source=
  'explicit'``, because the user saying it outright is the strongest evidence there
  is.

The tool descriptions lean hard on that distinction, since the failure mode to
avoid is Amber calling `remember_fact` on every turn and duplicating the writer.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.memory.store import SOURCE_EXPLICIT, TIER_DURABLE, get_store
from app.tools.registry import registry

# Facts are surfaced with ids so the model can act on one without a second lookup.
_MAX_SEARCH_RESULTS = 20


def _memory_on() -> bool:
    return get_settings().feature_memory


def _format(facts: list[dict]) -> str:
    return "\n".join(f"#{f['id']}: {f['content']}" for f in facts)


@registry.register(
    name="search_memory",
    description=(
        "Search everything you remember about the user, beyond the facts already in "
        "your context. Use it when they ask what you know about something, when you "
        "need a fact's id in order to correct or forget it, or when you suspect you "
        "know something that didn't surface this turn. Returns matching facts with "
        "their ids."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words to look for, e.g. 'dog' or 'where they live'.",
            },
            "limit": {
                "type": "integer",
                "description": "How many facts to return. Defaults to 5.",
            },
        },
        "required": ["query"],
    },
    available=_memory_on,
    read_only=True,
)
async def search_memory(query: str, limit: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        return "Error: search_memory needs something to search for."
    limit = max(1, min(int(limit or 5), _MAX_SEARCH_RESULTS))

    facts = await asyncio.to_thread(get_store().search_facts, query, limit)
    if not facts:
        return (
            f"Nothing in memory matches '{query}'. Say you don't know it rather "
            "than guessing."
        )
    return f"Facts matching '{query}':\n{_format(facts)}"


@registry.register(
    name="remember_fact",
    description=(
        "Deliberately save something about the user that you should still know "
        "weeks from now. Use it when they tell you directly ('remember that...', "
        "'my sister's name is...'), or when you learn something about them you'd be "
        "embarrassed to forget. Write it as one short self-contained sentence about "
        "them, like 'Prefers tea over coffee'. Do NOT use it for ordinary passing "
        "details — those are saved automatically — or for one-off requests, or for "
        "anything about yourself. Returns the fact's id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": (
                    "The fact as one short sentence about the user, e.g. "
                    "'Has a dog named Mango'."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional. One of: identity, preference, project, relationship, "
                    "commitment, pattern."
                ),
            },
        },
        "required": ["fact"],
    },
    available=_memory_on,
)
async def remember_fact(fact: str, category: str | None = None) -> str:
    fact = (fact or "").strip()
    if not fact:
        return "Error: remember_fact needs something to remember."

    store = get_store()
    # Ask before writing: `add_fact` reinforces an existing fact rather than
    # inserting a duplicate, so without this the reply would claim a new save for
    # something already known — and Amber would say "got it" when nothing changed.
    already = await asyncio.to_thread(store.fact_exists, fact)
    fact_id = await asyncio.to_thread(
        store.add_fact,
        fact,
        category,
        tier=TIER_DURABLE,
        confidence=0.9,
        source=SOURCE_EXPLICIT,
    )
    if fact_id is None:
        return "Error: remember_fact needs something to remember."
    if already:
        return f"Already knew that (#{fact_id}): {fact}"
    return f"Saved #{fact_id}: {fact}"


@registry.register(
    name="correct_fact",
    description=(
        "Replace something you remember with a corrected version. Use this the "
        "moment the user contradicts a fact in your memory — believe them, don't "
        "argue. The old version is kept on record but stops being used. Ids are "
        "shown in brackets beside the facts in your context; if you don't have one, "
        "call search_memory first. Returns the new id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "integer",
                "description": "The id of the fact that's wrong.",
            },
            "corrected": {
                "type": "string",
                "description": "The corrected fact, as one short sentence.",
            },
        },
        "required": ["fact_id", "corrected"],
    },
    available=_memory_on,
)
async def correct_fact(fact_id: int, corrected: str) -> str:
    corrected = (corrected or "").strip()
    if not corrected:
        return "Error: correct_fact needs the corrected version of the fact."

    store = get_store()
    old = await asyncio.to_thread(store.get_fact, int(fact_id))
    if old is None or old["status"] != "active":
        return (
            f"Error: no active fact #{fact_id} — call search_memory to find the "
            "right id."
        )

    new_id = await asyncio.to_thread(
        store.supersede_fact,
        int(fact_id),
        corrected,
        tier=TIER_DURABLE,
        confidence=0.9,
        source=SOURCE_EXPLICIT,
    )
    if new_id is None:
        return f"Error: couldn't replace fact #{fact_id}."
    return f"Replaced #{fact_id} ('{old['content']}') with #{new_id}: {corrected}"


@registry.register(
    name="forget_fact",
    description=(
        "Stop using something you remember, because it's wrong, out of date, or the "
        "user asked you to drop it. It won't come back on its own. If you're "
        "replacing it with something truer, use correct_fact instead so the "
        "correction is recorded. Ids are shown in brackets beside the facts in your "
        "context; if you don't have one, call search_memory first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "fact_id": {
                "type": "integer",
                "description": "The id of the fact to forget.",
            }
        },
        "required": ["fact_id"],
    },
    available=_memory_on,
)
async def forget_fact(fact_id: int) -> str:
    forgotten = await asyncio.to_thread(get_store().forget_fact, int(fact_id))
    if forgotten is None:
        return (
            f"Error: no active fact #{fact_id} — call search_memory to find the "
            "right id."
        )
    # Echo the content back so Amber can confirm what she dropped, out loud.
    return f"Forgotten #{fact_id}: {forgotten['content']}"


@registry.register(
    name="list_reminders",
    description="List the user's pending reminders, with their ids.",
    input_schema={"type": "object", "properties": {}},
    available=_memory_on,
    read_only=True,
)
async def list_reminders() -> str:
    reminders = await asyncio.to_thread(get_store().pending_reminders)
    if not reminders:
        return "There are no pending reminders."
    lines = [
        f"#{r['id']}: {r['text']}" + (f" (at {r['remind_at']})" if r["remind_at"] else "")
        for r in reminders
    ]
    return "Pending reminders:\n" + "\n".join(lines)


@registry.register(
    name="complete_reminder",
    description=(
        "Mark a reminder done once the user has dealt with it. If you don't know "
        "the id, call list_reminders first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reminder_id": {
                "type": "integer",
                "description": "The id of the reminder to clear.",
            }
        },
        "required": ["reminder_id"],
    },
    available=_memory_on,
)
async def complete_reminder(reminder_id: int) -> str:
    done = await asyncio.to_thread(get_store().complete_reminder, int(reminder_id))
    if not done:
        return (
            f"Error: no pending reminder #{reminder_id} — call list_reminders to "
            "find the right id."
        )
    return f"Cleared reminder #{reminder_id}."


__all__ = [
    "search_memory",
    "remember_fact",
    "correct_fact",
    "forget_fact",
    "list_reminders",
    "complete_reminder",
]
