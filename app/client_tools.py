"""Client-declared tools — capabilities a *client* runs on its own device.

Amber's own tools (``app.tools``) run inside this process. But a client often has
abilities only it can perform: show text on a screen, play a sound, flash an LED,
vibrate. This module lets a client *declare* such tools over the WebSocket; Amber
then offers them to the model — alongside its own — and, when the model calls one,
hands the request *back* to that client and waits for its result.

The mechanism, end to end:

1. The client sends a ``register_tools`` frame listing its tools. Each name is
   auto-prefixed with ``client_`` (so ``display_text`` becomes ``client_display_text``)
   — the prefix both namespaces them away from server tools and tells the brain to
   route the call back to the client rather than the registry.
2. The brain offers ``schemas()`` to the model and dispatches any ``client_*``
   ``tool_use`` through ``call()``.
3. ``call()`` mints a correlation id, sends a ``tool_call`` frame to the client, and
   awaits the matching ``tool_result`` (resolved by ``resolve()`` from the WS receive
   loop). It never raises into the brain — a timeout or a missing client degrades to
   an error string the model can react to, exactly like the server-side registry.

State is **per connection**: one ``ClientTools`` lives on each ``Session``. Declared
tool specs persist across a reconnect (until re-declared); the live send channel and
any in-flight calls are connection-scoped and torn down on disconnect via
``unbind()``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app import protocol
from app.config import get_settings

logger = logging.getLogger(__name__)

SendJson = Callable[[dict], Awaitable[None]]

# The brain routes any tool whose name starts with this prefix back to the client.
CLIENT_PREFIX = "client_"

# Anthropic tool names allow [a-zA-Z0-9_-], up to 64 chars. We sanitize client
# names to that set so a careless client can't produce an invalid schema.
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_name(name: str) -> str:
    """Coerce a client tool name into the Anthropic-allowed character set."""
    clean = _NAME_OK.sub("_", name.strip())
    if not clean.startswith(CLIENT_PREFIX):
        clean = CLIENT_PREFIX + clean
    return clean[:64]


class ClientTools:
    """Per-connection registry of a client's own tools + the call/result plumbing."""

    def __init__(self) -> None:
        # name -> Anthropic tool schema (name already prefixed/sanitized).
        self._specs: dict[str, dict[str, Any]] = {}
        # correlation id -> future awaiting the client's tool_result.
        self._pending: dict[str, asyncio.Future[str]] = {}
        # The current connection's JSON sender; None when no client is attached.
        self._send: SendJson | None = None
        self._counter = 0

    # --- connection lifecycle -------------------------------------------------

    def bind(self, send: SendJson) -> None:
        """Attach the live connection's sender so ``call`` can reach the client."""
        self._send = send

    def unbind(self) -> None:
        """Detach on disconnect: drop the sender and fail any in-flight calls.

        Tool specs are kept so a reconnecting session still advertises them until
        the client re-declares. Pending futures are cancelled — a turn awaiting one
        is being torn down anyway (the connection is gone).
        """
        self._send = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    # --- declaration ----------------------------------------------------------

    def register(self, tools: Any) -> list[str]:
        """Replace the client's declared tools from a ``register_tools`` payload.

        ``tools`` is the raw ``tools`` list off the frame. Invalid entries are
        skipped (and logged), names are sanitized + ``client_``-prefixed, and the
        set is capped at ``max_client_tools``. Returns the accepted tool names.
        """
        if not isinstance(tools, list):
            logger.warning("register_tools: 'tools' is not a list; ignoring")
            return []

        cap = get_settings().max_client_tools
        specs: dict[str, dict[str, Any]] = {}
        for entry in tools:
            spec = self._coerce_spec(entry)
            if spec is None:
                continue
            specs[spec["name"]] = spec  # last writer wins on duplicate names
            if cap > 0 and len(specs) >= cap:
                if len(tools) > cap:
                    logger.warning(
                        "register_tools: capping at max_client_tools=%d (got %d)",
                        cap,
                        len(tools),
                    )
                break

        self._specs = specs
        return list(self._specs)

    @staticmethod
    def _coerce_spec(entry: Any) -> dict[str, Any] | None:
        """Validate one declared tool into an Anthropic schema, or ``None``."""
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        schema = entry.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        return {
            "name": _sanitize_name(name),
            "description": str(entry.get("description") or ""),
            "input_schema": schema,
        }

    # --- what the brain reads -------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        """Anthropic schemas for every currently declared client tool."""
        return list(self._specs.values())

    def handles(self, name: str) -> bool:
        """True if ``name`` is one of this client's declared tools."""
        return name in self._specs

    # --- dispatch (server -> client -> server) --------------------------------

    async def call(self, name: str, tool_input: dict[str, Any] | None) -> str:
        """Run a client tool: send a ``tool_call``, await the ``tool_result``.

        Never raises (except ``CancelledError`` on interrupt) — a missing client,
        timeout, or client-reported failure becomes a descriptive string so the
        model can recover within the same turn, mirroring the server registry.
        """
        if name not in self._specs:
            return f"Error: tool {name!r} is not available."
        if self._send is None:
            return f"Error: the client that provides {name} isn't connected."

        settings = get_settings()
        self._counter += 1
        call_id = f"c{self._counter}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[call_id] = future

        try:
            logger.info("client tool call: %s(%s) id=%s", name, tool_input, call_id)
            await self._send(protocol.tool_call(call_id, name, tool_input or {}))
            return await asyncio.wait_for(
                future, timeout=settings.client_tool_timeout_s
            )
        except asyncio.TimeoutError:
            logger.warning("client tool %s timed out (id=%s)", name, call_id)
            return f"Error: the client didn't respond to {name} in time."
        except asyncio.CancelledError:
            raise  # interrupt/barge-in or disconnect — let the turn unwind
        finally:
            self._pending.pop(call_id, None)

    def resolve(self, call_id: Any, content: Any, is_error: bool = False) -> None:
        """Deliver a client's ``tool_result`` to the waiting ``call`` (if any).

        Called from the WS receive loop. Unknown/late ids are ignored, so a stray
        or duplicate result can't crash the connection.
        """
        if not isinstance(call_id, str):
            return
        future = self._pending.get(call_id)
        if future is None or future.done():
            logger.debug("Dropping tool_result for unknown/done id=%r", call_id)
            return
        text = str(content)
        future.set_result(f"Error from client: {text}" if is_error else text)
