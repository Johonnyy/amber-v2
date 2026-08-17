"""Asking permission — the half of `requires_confirmation` that never existed.

`agent-mcp-py` has always been able to *enforce* approval: a tool declared
``requires_confirmation`` refuses unless the call carries ``X-Confirmed: true``, and
`agent-runtime` publishes the flag on every tool schema, local and peer alike. What was
missing was anything that could produce that header honestly. Amber could describe a
risky action; she could not ask about one. So the flag was unusable in both directions —
`app/mcp_server.py` says so outright, and the reason it gives is exact: marking a tool
would have made it *permanently uncallable* rather than safely gated.

This is the source. Two frames, ``confirm_request`` out and ``confirm_response`` back,
and a `Confirmations` object that lives on the `Session` exactly the way `ClientTools`
does — which is what makes it work at all. The receive loop already holds the session, so
`resolve` is reachable from it; `bind`/`unbind` happen at the same two lines in
`voice_socket` as their client-tool counterparts.

**It fails closed, and that is the whole design.** No client bound, no answer inside the
timeout, or an explicit denial are one outcome: the tool does not run, and the model is
told so in a string it can react to within the turn. Silence is never approval — which is
why this is a *request* like ``tool_call`` and emphatically not a report like
``activity``. `agent-mcp-py` takes the same posture one layer down, where only ``true``
and ``1`` count and a well-meaning ``yes`` does not.

The gate itself is `ConfirmBroker`, a wrapper around the assembled broker in the shape
`app.activity.ActivityBroker` established. It reads ``x_agent.requires_confirmation`` off
`list_tools`, so a peer's declaration is honoured identically to one of Amber's own — the
model sees one flat tool list and this is the only place the distinction survives.

Propagating an approval to a peer needs one piece of care. `MCPClient.bind` is
**run-scoped**: it sets conversation id, depth and confirmed together, so binding
``confirmed=True`` alone would quietly reset the other two, detaching the call from its
conversation and resetting the depth guard to zero. So the broker remembers what it was
bound with and re-binds the whole set around a single approved call. That is safe because
the runner executes tool calls sequentially and Amber rebuilds the broker every turn —
both verified, and both worth re-checking if `agent-runtime` ever parallelises tool
execution.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app import protocol
from app.config import get_settings
from app.peers import classify_tool_name

logger = logging.getLogger(__name__)

SendJson = Callable[[dict], Awaitable[None]]

#: The three ways an approval can fail to happen. They are kept apart because the model
#: can act on the difference: a refusal is settled and should not be raised again, while
#: silence is worth one "did you still want me to?" — and being told "nobody is here" is
#: the only one that explains why nothing appeared on screen.
APPROVED = "approved"
DENIED = "denied"
TIMED_OUT = "timed_out"
NO_ONE = "no_client"

#: Deliberately descriptive rather than a bare failure: within the same turn Amber can
#: say what she needs instead of inventing a reason the tool didn't work.
MESSAGES = {
    DENIED: (
        "Error: {name} was not approved, so it was not run. Don't try again unless "
        "the user asks; acknowledge that you've left it alone."
    ),
    NO_ONE: (
        "Error: {name} needs human approval and nobody is connected to give it. "
        "Tell the user this needs confirming when they're back."
    ),
    TIMED_OUT: (
        "Error: no approval came back for {name} in time, so it was not run. "
        "Ask the user whether they still want it done."
    ),
}


class Confirmations:
    """Per-connection approval channel: ask over the socket, await the answer.

    Structurally a twin of `app.client_tools.ClientTools` — mint a correlation id, send a
    frame, await a future the receive loop resolves — because it is the same problem, and
    a second shape for it would be a second set of edge cases around timeouts, unbinding
    and late replies.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._send: SendJson | None = None
        self._counter = 0
        # Which connection the current sender belongs to. This object lives on the
        # `Session`, which outlives its socket — so a reconnect with `?session_id=` can
        # bind a new sender while the previous handler is still in its `finally`.
        self._token: object = None

    # --- connection lifecycle -------------------------------------------------

    def bind(self, send: SendJson, *, token: object = None) -> None:
        """Attach the live connection's sender so `request` can reach a human."""
        self._send = send
        self._token = token

    def unbind(self, *, token: object = None) -> None:
        """Detach on disconnect and refuse everything still waiting.

        Resolving to "not approved" rather than cancelling: the turn may still be
        unwinding, and "the human vanished" has a correct answer — no — which is more
        useful to the model than a cancellation it cannot describe.

        Ignored when a *newer* connection has already bound. Without that check, a
        resumed session would disarm the socket that just arrived, and every gated tool
        would refuse itself with "nobody is connected" while someone was plainly sat
        there looking at it.
        """
        if token is not None and self._token is not None and self._token is not token:
            logger.debug("Stale confirmation unbind ignored")
            return
        self._send = None
        self._token = None
        for future in self._pending.values():
            if not future.done():
                future.set_result(False)
        self._pending.clear()

    def connected(self) -> bool:
        return self._send is not None

    # --- ask ------------------------------------------------------------------

    async def request(
        self, name: str, tool_input: dict[str, Any] | None, *, origin: str
    ) -> str:
        """Ask the human. Returns `APPROVED`, `DENIED`, `TIMED_OUT` or `NO_ONE`.

        Only `APPROVED` runs the tool; the rest exist so the model is told *why* nothing
        happened, which is the difference between "you said no" and "I never heard back".

        Never raises except `asyncio.CancelledError` — a barge-in or a disconnect must
        unwind the turn normally.
        """
        if self._send is None:
            return NO_ONE

        settings = get_settings()
        self._counter += 1
        call_id = f"k{self._counter}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[call_id] = future

        try:
            logger.info("Asking for approval: %s (id=%s)", name, call_id)
            await self._send(
                protocol.confirm_request(
                    call_id, name, origin=origin, tool_input=tool_input or {}
                )
            )
            approved = await asyncio.wait_for(
                future, timeout=settings.confirm_timeout_s
            )
            return APPROVED if approved else DENIED
        except asyncio.TimeoutError:
            logger.warning("No approval for %s within the timeout (id=%s)", name, call_id)
            return TIMED_OUT
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken socket is a refusal, not a crash
            logger.debug("Could not ask for approval of %s", name, exc_info=True)
            return NO_ONE
        finally:
            self._pending.pop(call_id, None)

    def resolve(self, call_id: Any, approved: bool) -> None:
        """Deliver a ``confirm_response`` to the waiting `request`, if any.

        Called from the WS receive loop. Unknown or late ids are dropped, so a stray or
        duplicate answer cannot crash the connection — nor approve a different call than
        the one the human was looking at.
        """
        if not isinstance(call_id, str):
            return
        future = self._pending.get(call_id)
        if future is None or future.done():
            logger.debug("Dropping confirm_response for unknown/done id=%r", call_id)
            return
        future.set_result(bool(approved))


class ConfirmBroker:
    """A `agent_runtime.ToolBroker` that gates the broker it wraps on human approval.

    Transparent for every tool that doesn't declare ``requires_confirmation`` — which is
    all of them by default, so switching this on changes nothing until something is
    marked.
    """

    def __init__(
        self,
        inner: Any,
        confirmations: Confirmations,
        *,
        peers: tuple[str, ...] = (),
    ) -> None:
        self._inner = inner
        self._confirmations = confirmations
        self._peers = tuple(peers)
        self._gated: dict[str, bool] = {}
        # What `bind` was last called with, so an approved call can add ``confirmed``
        # without dropping the conversation id and depth that came with it.
        self._bound: dict[str, Any] = {}

    # -- ToolBroker ------------------------------------------------------------

    def bind(self, **kwargs: Any) -> None:
        self._bound = dict(kwargs)
        bind = getattr(self._inner, "bind", None)
        if bind is not None:
            bind(**kwargs)

    async def list_tools(self) -> list[dict[str, Any]]:
        schemas = await self._inner.list_tools()
        # Defensive to the point of paranoia, for the reason `ActivityBroker` documents:
        # the runner swallows any exception from here and carries on with an empty tool
        # list, so a stray KeyError in this bookkeeping would silently disarm every tool
        # Amber has and leave only a log line behind.
        for schema in schemas:
            try:
                name = schema["function"]["name"]
                flags = schema.get("x_agent") or {}
                self._gated[name] = bool(flags.get("requires_confirmation", False))
            except Exception:  # noqa: BLE001
                continue
        return schemas

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        if not self._gated.get(name):
            return await self._inner.call_tool(name, args)

        origin = classify_tool_name(name, self._peers)
        outcome = await self._confirmations.request(name, args, origin=origin)
        if outcome != APPROVED:
            logger.warning("Refusing %s: %s", name, outcome)
            return MESSAGES[outcome].format(name=name)

        logger.info("Approved: %s", name)
        return await self._call_confirmed(name, args)

    async def aclose(self) -> None:
        """Pass the close down the chain.

        Every wrapper in this stack has to forward this or it stops at whichever one
        forgot: `ActivityBroker.aclose` sits outside this and `MCPClient.aclose` inside,
        so a missing method here would silently strand a peer's MCP session on every
        turn that opened one. Harmless today only because the runner does not close a
        broker it was handed — which is not a property worth depending on.
        """
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    async def _call_confirmed(self, name: str, args: dict[str, Any]) -> str:
        """Run an approved call with ``X-Confirmed`` set for the duration.

        The flag is run-scoped upstream, not per-call, so it is toggled around this one
        call and restored afterwards. Re-binding the *whole* recorded set matters:
        `MCPClient.bind` assigns conversation id and depth from its own defaults, so
        passing ``confirmed`` alone would silently detach the call from its conversation
        and reset the depth guard.

        Safe because the runner executes tool calls one at a time and Amber builds a
        fresh broker per turn. If either ever changes, this needs a per-call header
        instead.
        """
        bind = getattr(self._inner, "bind", None)
        if bind is None:
            return await self._inner.call_tool(name, args)
        try:
            bind(**{**self._bound, "confirmed": True})
        except Exception:  # noqa: BLE001 — an unbindable broker is still callable
            logger.debug("Could not set the confirmed flag for %s", name, exc_info=True)
            return await self._inner.call_tool(name, args)
        try:
            return await self._inner.call_tool(name, args)
        finally:
            try:
                bind(**self._bound)
            except Exception:  # noqa: BLE001
                logger.debug("Could not clear the confirmed flag", exc_info=True)


def wrap(
    broker: Any,
    confirmations: Confirmations | None,
    *,
    peers: tuple[str, ...] = (),
    enabled: bool = True,
) -> Any:
    """Gate `broker` on approval, or hand it back untouched.

    Three ways this is a no-op, centralised here rather than at the call site: the
    feature is off, there is no broker, or this connection has no approval channel.
    """
    if broker is None or confirmations is None or not enabled:
        return broker
    return ConfirmBroker(broker, confirmations, peers=peers)
