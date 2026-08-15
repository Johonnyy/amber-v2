"""Amber's inline tools.

Fast and local to Amber, each an ordinary Python call — she never makes an HTTP
request to herself to add a task:

* ``web_search`` — a single lookup, not a research engine.
* ``read_url`` — the full text of one page, usually a search result.
* the task tools (``add_task`` / ``list_tasks`` / ``complete_task``).
* the reminder tools (``set_reminder`` / ``list_reminders`` / ``complete_reminder``).
* the memory tools (``search_memory`` / ``remember_fact`` / ``correct_fact`` /
  ``forget_fact``) — how Amber curates what she remembers, as opposed to the
  automatic writer that captures what was merely said. Only offered when memory is on.
* ``recall_recent`` — search or replay recent durable conversations on demand; only
  offered when memory is on.
* ``update_server`` — run the deploy update script; only offered when configured.

The inline-vs-delegated distinction the design turns on is still live, but the far
end changed: heavier work (calendar, email, files, browsing) now goes to another
agent's **MCP server**, configured as a peer in ``AMBER_MCP_PEERS`` and merged in
by `app.brain.build_broker`. The OpenClaw HTTP bridge that used to occupy that role
is gone.

Client-declared tools (tools a *client* can run on its own device) are a separate
mechanism that lives outside this registry — see ``app.client_tools`` — because
they are per-connection and dispatched back over the WebSocket, not process-wide.

Importing this package *registers* every tool on the shared ``registry`` — the
submodule imports below run the ``@registry.register`` decorators. The brain pulls
schemas and dispatches calls through the two helpers exported here, so the rest of
the app depends only on ``app.tools`` and never reaches into individual modules.

There is no longer a ``get_server_tool_schemas``: server tools ran inside
Anthropic's own request loop, and the OpenAI-compatible endpoint the brain now
speaks has no equivalent. See `app.tools.search` for what that cost.
"""

from __future__ import annotations

from typing import Any

# Importing the submodules is what populates the registry. Order is irrelevant.
from app.tools import (  # noqa: F401
    fetch,
    memory_tools,
    recall,
    reminders,
    search,
    tasks,
    update,
)
from app.tools.registry import Tool, ToolRegistry, registry


def get_tool_schemas() -> list[dict[str, Any]]:
    """Anthropic-format schemas for every currently-available tool Amber dispatches."""
    return registry.schemas()


async def run_tool(name: str, tool_input: dict[str, Any] | None) -> str:
    """Execute a tool by name; returns the ``tool_result`` content string."""
    return await registry.dispatch(name, tool_input)


__all__ = [
    "registry",
    "Tool",
    "ToolRegistry",
    "get_tool_schemas",
    "run_tool",
]
