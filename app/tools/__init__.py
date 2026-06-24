"""Amber's tools (Phase 4) — inline tools + the OpenClaw bridge.

Two kinds, by the load-bearing distinction in the design:

* **Inline tools** — fast and local to Amber: ``web_search``, the task tools
  (``add_task`` / ``list_tasks`` / ``complete_task``), ``set_reminder``,
  ``recall_recent`` (read recent durable conversations on demand; only offered when
  memory is on), and ``update_server`` (run the deploy update script; only offered
  when configured).
* **OpenClaw bridge** — ``delegate_to_openclaw`` for anything heavier (calendar,
  email, files, browsing), sent over HTTP to a separate service and awaited.

Client-declared tools (tools a *client* can run on its own device) are a separate
mechanism that lives outside this registry — see ``app.client_tools`` — because
they are per-connection and dispatched back over the WebSocket, not process-wide.

Importing this package *registers* every tool on the shared ``registry`` — the
submodule imports below run the ``@registry.register`` decorators. The brain pulls
schemas and dispatches calls through the two helpers exported here, so the rest of
the app depends only on ``app.tools`` and never reaches into individual modules.
"""

from __future__ import annotations

from typing import Any

# Importing the submodules is what populates the registry. Order is irrelevant.
from app.tools import openclaw, recall, reminders, search, tasks, update  # noqa: F401
from app.tools.registry import Tool, ToolRegistry, registry


def get_tool_schemas() -> list[dict[str, Any]]:
    """Anthropic-format schemas for every currently-available tool Amber dispatches."""
    return registry.schemas()


def get_server_tool_schemas() -> list[dict[str, Any]]:
    """Schemas for Anthropic-executed *server* tools (e.g. native web search).

    Added to the brain's ``tools=[...]`` list but never dispatched here — Anthropic
    runs them server-side and streams the results back inline. See
    :func:`app.tools.search.server_tool_schemas`.
    """
    return search.server_tool_schemas()


async def run_tool(name: str, tool_input: dict[str, Any] | None) -> str:
    """Execute a tool by name; returns the ``tool_result`` content string."""
    return await registry.dispatch(name, tool_input)


__all__ = [
    "registry",
    "Tool",
    "ToolRegistry",
    "get_tool_schemas",
    "get_server_tool_schemas",
    "run_tool",
]
