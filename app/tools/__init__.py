"""Amber's inline tools.

Fast and local to Amber, each an ordinary Python call — she never makes an HTTP
request to herself to add a task:

* ``web_search`` — a single lookup, not a research engine.
* the task tools (``add_task`` / ``list_tasks`` / ``complete_task``).
* ``set_reminder``.
* ``recall_recent`` — read recent durable conversations on demand; only offered
  when memory is on.
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
from app.tools import recall, reminders, search, tasks, update  # noqa: F401
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
