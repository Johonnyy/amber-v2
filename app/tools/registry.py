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
  predicate (the memory tools need ``feature_memory``; ``update_server`` needs a
  configured command); unavailable tools are hidden from ``schemas()`` and refuse
  to ``dispatch``, so the model is never offered a tool it can't actually use.
* **Query tools are marked ``read_only``**, the ecosystem convention — surfaced in
  the schema as ``x_agent.read_only``, matching what `agent_runtime`'s
  ``LocalToolBroker`` already emits.
* **Results and errors are clamped.** A tool result is context the model pays for
  on every later turn of the same exchange, so neither a runaway page of output nor
  a library traceback repr can poison the rest of it.
* **Every call is timed and recorded** (see `app.signals`). One choke point, so no
  tool has to remember to instrument itself.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Union

logger = logging.getLogger(__name__)

# A tool is sync or async; both return the string fed back to the model.
ToolFunc = Callable[..., Union[Awaitable[str], str]]


def _always() -> bool:
    return True


# A tool result is context the model pays for on every subsequent turn of the same
# exchange, so both of these are backstops against one bad call poisoning a turn: a
# runaway page of search output, or an httpx traceback repr pasted in as an "error".
MAX_RESULT_CHARS = 8000
MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    func: ToolFunc
    available: Callable[[], bool] = field(default=_always)
    read_only: bool = False

    def schema(self) -> dict[str, Any]:
        """This tool as an Anthropic tool-definition dict.

        Query tools carry ``x_agent.read_only``, the ecosystem-wide convention from
        CLAUDE.md — the same shape `agent_runtime`'s ``LocalToolBroker`` emits, so a
        caller can tell what's safe to retry without knowing which broker answered.
        """
        schema: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
        if self.read_only:
            schema["x_agent"] = {"read_only": True}
        return schema


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
        read_only: bool = False,
    ) -> Callable[[ToolFunc], ToolFunc]:
        """Decorator: register the wrapped function as a tool.

        ``input_schema`` is a JSON Schema object for the tool's arguments.
        ``available`` is an optional predicate evaluated at schema/dispatch time;
        when it returns falsey the tool is hidden and won't run. ``read_only`` marks
        a query tool — it changes nothing, so it's always safe to call or retry.
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
                read_only=read_only,
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

        # Timed here because this is the one place every tool passes through, so
        # nothing has to remember to instrument itself. See app.signals.
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            result = tool.func(**(tool_input or {}))
            if inspect.isawaitable(result):
                result = await result
            text = _clamp(str(result), MAX_RESULT_CHARS, "result truncated")
            _record(name, ok=not text.startswith("Error:"), latency_ms=elapsed_ms())
            return text
        except asyncio.CancelledError:
            raise  # interrupt/barge-in — let the turn unwind
        except Exception as exc:  # noqa: BLE001 — a tool must not crash the turn
            logger.exception("Tool %s failed", name)
            _record(
                name,
                ok=False,
                latency_ms=elapsed_ms(),
                detail=type(exc).__name__,
            )
            # The full exception is in the log; the model gets a bounded summary.
            # An unclamped repr here can paste an entire library traceback into the
            # prompt, where it costs tokens on every later turn of the exchange and
            # tells the model nothing it can act on.
            detail = _clamp(str(exc), MAX_ERROR_CHARS, "…")
            return f"Error running {name}: {detail}"


def _clamp(text: str, limit: int, marker: str) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[{marker}]"


def _record(name: str, **fields) -> None:
    """Log a tool call to telemetry, without letting telemetry break a tool call.

    Imported lazily: `app.signals` imports the memory store, which imports config,
    and a module-level import here would tie the tool registry into that cycle.
    """
    try:
        from app import signals

        signals.record(signals.KIND_TOOL_CALL, name=name, **fields)
    except Exception:  # noqa: BLE001 — telemetry is never worth a failed turn
        logger.debug("Could not record tool signal", exc_info=True)


# The process-wide registry. Tool modules populate it on import (see app/tools/
# __init__.py), which is what makes `from app.tools import registry` complete.
registry = ToolRegistry()
