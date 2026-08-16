"""The brain — a streamed token source, driven by `agent_runtime`.

The contract is unchanged and deliberately so: ``think()`` returns an
``AsyncIterator[str]`` of text deltas, exactly as `app.responder.respond` does, so
the sentence splitter downstream can start TTS on the first sentence before the
whole response exists. Everything from the splitter onwards is untouched.

What changed underneath is the loop. Amber used to drive the Anthropic Messages API
herself — stream a turn, spot ``tool_use``, run the tools, feed results back,
repeat — and that loop is the thing every agent in the ecosystem needs, so it now
lives in `agent_runtime` and Amber imports it. The `anthropic` SDK is gone; the
provider is OpenRouter, reached through the standard OpenAI-compatible client.

**Three kinds of tool, one broker stack.** The runner does not care where a tool
lives; it asks a `ToolBroker`. Amber composes hers in priority order:

1. *Her own registry* (`app.tools`) via `AnthropicRegistryBroker` — the same
   functions the voice path has always called, so a tool stays a plain Python call
   and Amber never makes an HTTP request to herself to add a task.
2. *Client-declared tools* (`app.client_tools`), routed back over the WebSocket to
   whatever device is connected. Same adapter, because `ClientTools` already
   exposes the schema/dispatch pair the adapter wants.
3. *The `expect_reply` signal*, which is not a tool at all — calling it just flips
   a flag on this turn's `TurnSignals` so the pipeline keeps the mic open.
4. *Peer MCP servers*, for heavy or delegated work — from `AMBER_MCP_PEERS` **and**
   from whatever has registered with the sync store, unioned by
   `agent_mcp.PeerRegistry` with the static map winning. Reading only the first was
   a bug with no symptom: an unlisted peer is not a failing tool, it is no tool.
   See `app.peers`.

Amber's own tools come first so a colliding name from a client or a peer can never
shadow them.

**What this costs relative to the Anthropic-native brain.** Server-side web search
(`web_search_20250305`) ran inside Anthropic's own request and has no equivalent on
the OpenAI-compatible endpoint, so `search_provider` falls back to the
self-dispatched `tavily` / `duckduckgo` providers. The `pause_turn` stop reason
went with it — it only ever existed to resume a server tool. The one piece worth
keeping was the newline flush at a tool boundary, and that now lives in
`agent_runtime` (`flush_on_tool_call`) where every voice agent gets it.

The runner is rebuilt per turn rather than cached, because the broker binds this
turn's client tools, signals and conversation id. That costs one small object;
caching it would mean leaking one turn's state into the next.

**Which model** is decided here too, and late. `think` takes a *keyword* — the
connection's own, or the install default — and `app.models.resolve` turns it into a
concrete OpenRouter id on the spot, because both halves move at runtime now: a client
can switch keyword between turns, and the keyword itself can be re-pointed at a
different model without a restart. Resolving any earlier would cache a decision that
is meant to be changeable.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from agent_runtime import (
    AgentRunner,
    AnthropicRegistryBroker,
    CompositeBroker,
    LocalToolBroker,
    MCPClient,
)
from agent_runtime import Settings as RuntimeSettings

# The peer record shape is agent-mcp-py's to define — it owns registration and
# resolution — so Amber parses her static peer list with its helper rather than
# inventing a second format that would drift.
from agent_mcp.registry import load_static_peers

from app import peers as peer_discovery
from app.config import Settings, get_settings
from app.models import resolve as resolve_model
from app.persona import SYSTEM_PROMPT
from app.tools import get_tool_schemas, run_tool

if TYPE_CHECKING:
    from app.client_tools import ClientTools
    from app.turn_signals import TurnSignals

logger = logging.getLogger(__name__)

# Turn-based conversations: a signaling tool the model calls to keep a turn open.
# Advertised like any other tool, but it does nothing except flip a flag — it is
# never routed to the registry.
EXPECT_REPLY_TOOL = "expect_reply"
_EXPECT_REPLY_DESCRIPTION = (
    "Signal that you're waiting for the user's answer and the conversation "
    "should stay open for their reply. Call this ONLY when you genuinely need "
    "them to respond — a real clarifying question, or a back-and-forth you're "
    "steering. Never for rhetorical questions, asides, or ordinary replies; "
    "most turns just end. Speak your question as normal text; this only keeps "
    "the mic open for their answer."
)


def runtime_settings(settings: Settings | None = None) -> RuntimeSettings:
    """Build `agent_runtime`'s settings from Amber's own.

    Injection rather than environment: `agent_runtime` would happily read
    ``AGENT_RUNTIME_*`` variables, but then Amber would have two config surfaces
    that could disagree — most damagingly about ``db_path``, where a mismatch means
    cost rows land in a file nobody looks at. One prefix, one source of truth.
    """
    settings = settings or get_settings()
    return RuntimeSettings(
        _env_file=None,
        openrouter_api_key=settings.openrouter_api_key,
        app_name="amber",
        # Resolved here, not passed as a keyword: Amber's keyword table is a superset
        # of the runtime's own three tiers, so handing "coding" to a library that has
        # never heard of it would raise `UnknownTier` on the fallback path.
        default_tier=resolve_model(settings.llm_tier, settings),
        max_tokens=settings.llm_max_tokens,
        max_steps=settings.max_tool_iterations,
        # Cost rows go in Amber's own database, beside her memory and the MCP
        # layer's tool log, so spend can be joined to what caused it.
        db_path=settings.memory_db_path,
        title="Amber",
    )


def _signal_broker(signals: "TurnSignals") -> LocalToolBroker:
    """A one-tool broker for ``expect_reply``.

    Modelled as a tool because that is the only channel the model has, but it does
    no work: it sets the flag and acknowledges. Keeping it in its own broker means
    the interception is structural rather than a special case buried in dispatch.
    """
    broker = LocalToolBroker()

    def expect_reply() -> str:
        signals.awaiting_response = True
        return "OK — I'll wait for their reply."

    broker.register(
        EXPECT_REPLY_TOOL,
        _EXPECT_REPLY_DESCRIPTION,
        {"type": "object", "properties": {}},
        expect_reply,
        read_only=True,
    )
    return broker


def build_broker(
    settings: Settings | None = None,
    *,
    client_tools: "ClientTools | None" = None,
    signals: "TurnSignals | None" = None,
):
    """Assemble this turn's tool broker, or ``None`` when there is nothing to offer.

    Order is priority order: Amber's own tools resolve first, so a client or peer
    that declares a colliding name cannot shadow them.
    """
    settings = settings or get_settings()
    brokers: list = []

    if settings.feature_tools:
        brokers.append(AnthropicRegistryBroker(get_tool_schemas, run_tool))

    if settings.feature_client_tools and client_tools is not None:
        # ClientTools already exposes exactly the schema/dispatch pair the adapter
        # wants, so the same adapter serves both. `schemas` is passed as a callable
        # because the connected device can redeclare its tools at any time.
        brokers.append(
            AnthropicRegistryBroker(client_tools.schemas, client_tools.call)
        )

    if settings.feature_turn_based and signals is not None:
        brokers.append(_signal_broker(signals))

    if settings.feature_tools:
        # Peers come from two places, and this used to read only one of them:
        #
        #     peers = load_static_peers(settings.mcp_peers, settings.mcp_peer_token)
        #     if peers:
        #         brokers.append(MCPClient(list(peers), resolver=peers))
        #
        # Both halves of that were wrong. `if peers:` meant an empty AMBER_MCP_PEERS
        # built no client *at all*, and passing the dict as `resolver` took
        # MCPClient._resolve's Mapping branch, which returns before it would ever
        # fall through to agent_mcp's registry. So the sync store's discovered layer
        # was unreachable either way — a peer could register perfectly, appear in
        # GET /servers, and be offered to the model as nothing whatsoever, with no
        # error anywhere, because an unlisted peer is not a failing tool, it is no
        # tool. See app/peers.py for the whole account.
        #
        # The static layer is re-asserted per turn rather than once at import: it is
        # cheap, and it keeps `AMBER_MCP_PEERS` authoritative over anything a
        # background pull has since discovered. That ordering is agent_mcp's contract
        # and worth honouring — pointing Amber at a local peer during an incident
        # must not be quietly undone by the next refresh.
        registry = peer_discovery.registry()
        registry.set_static(load_static_peers(settings.mcp_peers, settings.mcp_peer_token))
        # `known()` is the union of both layers; `resolve` is the two-layer lookup,
        # synchronous and I/O-free, so handing it over as a callable costs the turn
        # path nothing. A name that vanishes from the registry between here and the
        # call raises a clear KeyError rather than a NoneType crash.
        names = registry.known()
        if names:
            brokers.append(MCPClient(names, resolver=registry.resolve))

    if not brokers:
        return None
    if len(brokers) == 1:
        return brokers[0]
    return CompositeBroker(brokers)


async def think(
    messages: list[dict],
    system: str | None = None,
    *,
    client_tools: "ClientTools | None" = None,
    signals: "TurnSignals | None" = None,
    conversation_id: str | None = None,
    model: str | None = None,
) -> AsyncIterator[str]:
    """Stream Amber's reply for the given conversation history.

    ``messages`` is the running history as ``{"role", "content"}`` dicts — plain
    text on both sides, which is already what the OpenAI-compatible protocol wants,
    so no translation happens here. The system prompt is injected per turn rather
    than stored in the history; ``system`` is the persona prompt with the memory
    block appended (see `app.persona.compose_system_prompt`), falling back to the
    bare persona prompt.

    ``client_tools`` is the connection's declared client-side tools, offered
    alongside Amber's own and dispatched back over the WebSocket. ``signals`` is the
    per-turn back-channel: when the model calls ``expect_reply`` the flag flips and
    the pipeline keeps the turn open. ``conversation_id`` is the session id, tying
    this turn's model spend and tool calls together in the usage tables and
    forwarded to any peer agent Amber calls.

    ``model`` is this connection's chosen keyword (`app.models`) — ``None`` uses the
    install's ``llm_tier``. It is resolved to a concrete model id here, at the last
    possible moment, so a keyword re-pointed between two turns takes effect on the
    second one without anything being invalidated or restarted.

    Yields text deltas as they arrive. Tool round trips happen inside and are
    invisible here, apart from a newline at each tool boundary so speech before a
    tool reaches TTS without waiting for the tool to finish.
    """
    settings = get_settings()
    system = system if system is not None else SYSTEM_PROMPT
    broker = build_broker(settings, client_tools=client_tools, signals=signals)

    keyword = model or settings.llm_tier
    resolved = resolve_model(keyword, settings)

    runner = AgentRunner(
        model=resolved,
        broker=broker,
        settings=runtime_settings(settings),
    )

    logger.debug(
        "LLM: %d message(s), tools=%s -> %s (%s)",
        len(messages),
        broker is not None,
        resolved,
        keyword,
    )

    async for text in runner.stream(
        messages, system=system, conversation_id=conversation_id
    ):
        yield text
