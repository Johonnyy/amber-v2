"""Task tools — add, list, and complete the user's to-dos.

The lightweight, inline kind of tool: they touch only Amber's own SQLite store, so
they're fast and need no network (anything heavier goes to a peer MCP server). The
context builder already *surfaces* open tasks into the prompt each turn, with their
ids; these tools let the model *mutate* the list during a turn.

Store access goes through ``get_store()`` (the process singleton) wrapped in
``asyncio.to_thread`` so the synchronous SQLite call never blocks the event loop.
"""

from __future__ import annotations

import asyncio

from app.memory.store import get_store
from app.tools.registry import registry


@registry.register(
    name="add_task",
    description=(
        "Add a to-do item to the user's task list. Use when they ask you to track "
        "something they need to do. If they gave a specific time to be reminded "
        "at, use set_reminder instead. For a fact about the user rather than "
        "something to do, use remember_fact."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "The task as a short phrase, e.g. 'buy milk'.",
            }
        },
        "required": ["description"],
    },
)
async def add_task(description: str) -> str:
    description = (description or "").strip()
    if not description:
        return "Error: a task needs a description."
    task_id = await asyncio.to_thread(get_store().add_task, description)
    return f"Added task #{task_id}: {description}"


@registry.register(
    name="list_tasks",
    description=(
        "List the user's open (not-yet-done) tasks, with their ids. The current "
        "ones are usually already in your context — call this only when the user "
        "asks for the whole list, or when you need an id you don't have."
    ),
    input_schema={"type": "object", "properties": {}},
    read_only=True,
)
async def list_tasks() -> str:
    tasks = await asyncio.to_thread(get_store().open_tasks)
    if not tasks:
        return "There are no open tasks."
    lines = "\n".join(f"#{t['id']}: {t['description']}" for t in tasks)
    return f"Open tasks:\n{lines}"


@registry.register(
    name="complete_task",
    description=(
        "Mark one of the user's tasks done, by its numeric id. If you don't know "
        "the id, call list_tasks first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "The id of the task to mark done.",
            }
        },
        "required": ["task_id"],
    },
)
async def complete_task(task_id: int) -> str:
    ok = await asyncio.to_thread(get_store().complete_task, int(task_id))
    if ok:
        return f"Marked task #{task_id} done."
    return (
        f"Error: no open task #{task_id} — call list_tasks to find the right id."
    )
