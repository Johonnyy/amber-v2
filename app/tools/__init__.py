"""Amber's inline tools.

Fast and local to Amber: ``web_search``, the task tools (``add_task`` /
``list_tasks`` / ``complete_task``), and ``set_reminder``. A tool here is an
ordinary Python call — Amber never makes an HTTP request to herself to add a task.

The inline-vs-delegated distinction the design turns on is still live, but the far
end changed: heavier work (calendar, email, files, browsing) now goes to another
agent's **MCP server**, configured as a peer in ``AMBER_MCP_PEERS`` and merged in
by `app.brain.build_broker`. The old OpenClaw HTTP bridge that used to occupy that
role is gone.

Importing this package *registers* every tool on the shared ``registry`` — the
submodule imports below run the ``@registry.register`` decorators. The brain pulls
schemas and dispatches calls through the two helpers exported here, so the rest of
the app depends only on ``app.tools`` and never reaches into individual modules.
"""

from __future__ import annotations

from typing import Any

# Importing the submodules is what populates the registry. Order is irrelevant.
from app.tools import reminders, search, tasks  # noqa: F401
from app.tools.registry import Tool, ToolRegistry, registry


def get_tool_schemas() -> list[dict[str, Any]]:
    """Anthropic-format schemas for every currently-available tool."""
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
