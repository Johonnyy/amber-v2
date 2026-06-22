"""Tool registry — the single source of truth for Amber's tools (Phase 4).

A tool is a Python function plus an Anthropic tool schema. The registry turns the
set of registered tools into two things the brain needs:

* ``schemas()`` — the ``tools=[...]`` list handed to the LLM, so the model can
  decide which (if any) to call.
* ``dispatch(name, input)`` — run the function behind a ``tool_use`` block and
  return its result as the string ``content`` of a ``tool_result``.

The brain owns the agentic loop (stream → ``tool_use`` → execute → continue); the
registry owns *what* the tools are and *how* one runs. Design rules:

* **A tool never raises into the brain.** ``dispatch`` converts any exception into
  an error string, so one broken tool degrades to "the model is told it failed"
  rather than taking down the turn. ``asyncio.CancelledError`` is re-raised so an
  interrupt/barge-in still unwinds cleanly.
* **A tool returns a plain string** — the text the model reads back.
* **Tools may be conditionally available.** A tool can carry an ``available``
  predicate (e.g. the OpenClaw bridge needs a configured URL); unavailable tools
  are hidden from ``schemas()`` and refuse to ``dispatch``, so the model is never
  offered a tool it can't actually use.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Union

logger = logging.getLogger(__name__)

# A tool is sync or async; both return the string fed back to the model.
ToolFunc = Callable[..., Union[Awaitable[str], str]]


def _always() -> bool:
    return True


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: ToolFunc
    available: Callable[[], bool] = field(default=_always)

    def schema(self) -> dict[str, Any]:
        """This tool as an Anthropic tool-definition dict."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """A name → :class:`Tool` map with schema export and safe dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        *,
        available: Callable[[], bool] | None = None,
    ) -> Callable[[ToolFunc], ToolFunc]:
        """Decorator: register the wrapped function as a tool.

        ``input_schema`` is a JSON Schema object for the tool's arguments.
        ``available`` is an optional predicate evaluated at schema/dispatch time;
        when it returns falsey the tool is hidden and won't run.
        """

        def decorator(func: ToolFunc) -> ToolFunc:
            if name in self._tools:
                raise ValueError(f"Tool already registered: {name!r}")
            self._tools[name] = Tool(
                name=name,
                description=description,
                input_schema=input_schema,
                func=func,
                available=available or _always,
            )
            return func

        return decorator

    def names(self) -> list[str]:
        """Every registered tool name (regardless of availability)."""
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """Anthropic schemas for every *currently available* tool."""
        return [t.schema() for t in self._tools.values() if t.available()]

    async def dispatch(self, name: str, tool_input: dict[str, Any] | None) -> str:
        """Run a tool by name; always returns a string, never raises (except
        ``CancelledError``). An unknown/unavailable tool or a tool error becomes a
        descriptive string so the model can recover within the same turn."""
        tool = self._tools.get(name)
        if tool is None or not tool.available():
            logger.warning("Tool unavailable or unknown: %s", name)
            return f"Error: tool {name!r} is not available."

        try:
            result = tool.func(**(tool_input or {}))
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        except asyncio.CancelledError:
            raise  # interrupt/barge-in — let the turn unwind
        except Exception as exc:  # noqa: BLE001 — a tool must not crash the turn
            logger.exception("Tool %s failed", name)
            return f"Error running {name}: {exc}"


# The process-wide registry. Tool modules populate it on import (see app/tools/
# __init__.py), which is what makes `from app.tools import registry` complete.
registry = ToolRegistry()
