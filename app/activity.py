"""Making a turn's tool calls visible to the client that asked for it.

Amber could always *do* things mid-turn and never say so. `app.brain` puts it
plainly — "tool round trips happen inside and are invisible here" — because
`think()` is an ``AsyncIterator[str]`` and the only trace of a tool call in that
stream is the newline `agent_runtime` flushes at the boundary. So a client had two
states to render, thinking and speaking, for a turn that might have run a web search,
corrected a memory and handed a two-minute build to Bloom in between.

The awkward part is that the two halves of the fix start in different places. The
pipeline owns ``send_json`` and cannot see tool calls; the brokers see every tool call
and have no socket. This module is the join: a broker that wraps another broker,
takes a sink, and reports what passes through.

WHY A WRAPPER RATHER THAN INSTRUMENTING `registry.dispatch`. That looks like the
obvious seam — it is already the one place every tool is timed, and it already writes
telemetry. But it is only *Amber's own* tools. A client tool goes through
`app.client_tools.call`, a peer tool through `agent_runtime.MCPClient.call_tool`, and
neither touches the registry. Instrumenting dispatch would have shown the searches
and hidden every `bloom__*` call — precisely the calls worth watching, since they are
the slow ones. `CompositeBroker.call_tool` is the real choke point: all four brokers
are behind it, so one wrapper covers all four classes of tool in one shape.

It also closes a telemetry hole on the way past. `KIND_TOOL_CALL` has only ever
covered registry tools, so no peer call has ever appeared in Amber's own signals
either. This records the ones dispatch cannot see — and deliberately not the ones it
can, or every local tool would be counted twice.

**Emission never costs a turn.** Every send is wrapped, and a failure is logged at
debug and dropped. That is the same posture `registry._record` takes toward
telemetry, and for the same reason: a socket that has gone away is not a reason to
fail a tool call whose result the model is waiting on.

**A barge-in still unwinds.** `asyncio.CancelledError` is re-raised after emitting a
closing frame, so the turn tears down exactly as it did before. Without the closing
frame a cancelled call would sit spinning in the UI forever, which is the one failure
mode a start/end protocol invites.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection
from typing import Any
from uuid import uuid4

from app import protocol
from app.peers import classify_tool_name
from app.tools.registry import _clamp

logger = logging.getLogger(__name__)

SendJson = Callable[[dict], Awaitable[None]]

#: What rides on the wire, which is not what the model gets. `registry.dispatch`
#: clamps a result to 8000 characters because that is a sane budget for something
#: about to re-enter a prompt; this is a preview for a person, and a card that can
#: be expanded to two thousand characters is already longer than anyone reads.
MAX_WIRE_ARGS = 2000
MAX_WIRE_RESULT = 2000

#: Origins whose calls this broker records to telemetry. `registry.dispatch` already
#: records its own, and ``signal`` is a flag flip rather than work — recording either
#: here would double-count the first and invent a tool call for the second.
_TELEMETERED = (protocol.ORIGIN_CLIENT,)


class ActivityBroker:
    """A `agent_runtime.ToolBroker` that narrates the broker it wraps.

    Transparent by construction: every call is delegated and every result returned
    unchanged, so wrapping is invisible to the runner and to the model. The only
    additions are the two frames per call.
    """

    def __init__(
        self,
        inner: Any,
        emit: SendJson,
        *,
        peers: Collection[str] = (),
    ) -> None:
        self._inner = inner
        self._emit = emit
        self._peers = tuple(peers)
        # Filled from `list_tools`, which the runner calls once per run before any
        # call_tool. Read-only is worth showing — a card for a query should not look
        # like a card for something that changed state — and this is the only place
        # it is available, since by call time the runner has just a name and args.
        self._read_only: dict[str, bool] = {}

    # -- ToolBroker ------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        schemas = await self._inner.list_tools()
        # Defensive to the point of paranoia, because the failure mode is invisible:
        # the runner's `_tool_schemas` swallows any exception from here and carries on
        # with an empty tool list, so a stray KeyError in this bookkeeping would
        # silently disarm every tool Amber has and leave only a log line behind.
        for schema in schemas:
            try:
                name = schema["function"]["name"]
                # `x_agent` is agent_runtime's own sidecar block, sibling to
                # `function` — stripped before the schema goes to the provider, so it
                # exists precisely for local readers like this one.
                flags = schema.get("x_agent") or {}
                self._read_only[name] = bool(flags.get("read_only", False))
            except Exception:  # noqa: BLE001 — never cost the turn its tools
                continue
        return schemas

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        call_id = uuid4().hex[:12]
        origin = classify_tool_name(name, self._peers)
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        await self._say(
            protocol.activity(
                call_id,
                protocol.PHASE_START,
                name,
                origin=origin,
                tool_input=_wire_args(args),
                read_only=self._read_only.get(name),
            )
        )

        try:
            result = str(await self._inner.call_tool(name, args))
        except asyncio.CancelledError:
            # Barge-in — and deliberately **no closing frame**, which took some
            # working out. Two things make an emit here actively harmful:
            #
            # `cancel_current` is also what runs when the *connection* closes, so the
            # socket may already be gone. A send on a dead socket raises
            # WebSocketDisconnect, and starlette latches the connection DISCONNECTED
            # on the way past, after which every later send raises RuntimeError —
            # including `run_turn`'s own unguarded `thinking(False)` in its finally.
            # And `start_turn` awaits `cancel_current` *before* creating the new turn,
            # so anything awaited here is latency between the user interrupting and
            # Amber listening again.
            #
            # Nothing is lost. The client is the one that sent `interrupt`, so it
            # already knows; a card still open when the turn ends is an interrupted
            # card, and it can say so without being told.
            raise
        except Exception as exc:  # noqa: BLE001 — mirror the contract below us
            # Brokers below turn errors into result strings rather than raising, so
            # this is the unexpected case. Report it and re-raise: swallowing it here
            # would hand the model a silent empty result.
            await self._say(
                protocol.activity(
                    call_id,
                    protocol.PHASE_END,
                    name,
                    origin=origin,
                    ok=False,
                    ms=elapsed_ms(),
                    result=_clamp(str(exc), MAX_WIRE_RESULT, "truncated"),
                )
            )
            raise

        # The same heuristic `registry.dispatch` uses, and the same caveat: it is a
        # convention every tool follows, not a guarantee. The full text rides along
        # so a client can show what actually came back rather than trust the flag.
        ok = not result.startswith("Error")
        ms = elapsed_ms()
        await self._say(
            protocol.activity(
                call_id,
                protocol.PHASE_END,
                name,
                origin=origin,
                ok=ok,
                ms=ms,
                result=_clamp(result, MAX_WIRE_RESULT, "result truncated"),
            )
        )
        _record(name, origin, ok=ok, latency_ms=ms)
        return result

    # -- duck-typed extras the runner and CompositeBroker look for --------------

    def bind(self, **kwargs: Any) -> None:
        """Forward the runner's per-run binding.

        Not part of the `ToolBroker` protocol — the runner reaches for it with
        ``getattr(broker, "bind", None)`` — which is exactly why it is easy to leave
        off a wrapper and exactly why doing so would be bad. ``bind`` is what carries
        ``conversation_id`` and ``depth`` to `MCPClient`, and depth is what stops
        agent-to-agent call loops. A wrapper that ate it would silently disable the
        hop limit.
        """
        bind = getattr(self._inner, "bind", None)
        if bind is not None:
            bind(**kwargs)

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    # -- internals -------------------------------------------------------------

    async def _say(self, frame: dict[str, Any]) -> None:
        """Emit one frame, and never let the wire break the turn."""
        try:
            await self._emit(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a closed socket is not a failed tool
            logger.debug("Could not emit activity frame", exc_info=True)


def _wire_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Clamp argument *values* for display, keeping the shape a UI wants.

    Per-value rather than one clamp over the whole object, because the shape is the
    useful part: a card renders ``slug: spotify-dj`` as a chip, and a system prompt
    two pages long should not take the other five keys off the screen with it.
    """
    if not args:
        return {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        out[key] = (
            _clamp(value, MAX_WIRE_ARGS, "truncated")
            if isinstance(value, str)
            else value
        )
    return out


def _record(name: str, origin: str, **fields: Any) -> None:
    """Record the tool calls `registry.dispatch` cannot see. Never raises.

    Lazily imported for the reason `app.tools.registry` documents: `app.signals`
    pulls in the memory store, and a module-level import would tie this into that
    cycle.
    """
    if origin not in _TELEMETERED and not origin.startswith(
        protocol.ORIGIN_PEER_PREFIX
    ):
        return
    try:
        from app import signals

        signals.record(signals.KIND_TOOL_CALL, name=name, **fields)
    except Exception:  # noqa: BLE001 — telemetry is never worth a failed turn
        logger.debug("Could not record activity signal", exc_info=True)


def wrap(
    broker: Any,
    emit: SendJson | None,
    *,
    peers: Collection[str] = (),
    enabled: bool = True,
) -> Any:
    """Wrap `broker` for reporting, or hand it back untouched.

    A single place for the three ways this is a no-op — the feature is off, there is
    no broker to wrap, or nobody is listening — so `app.brain` stays a list of
    brokers rather than a nest of conditionals.
    """
    if not enabled or broker is None or emit is None:
        return broker
    return ActivityBroker(broker, emit, peers=peers)
