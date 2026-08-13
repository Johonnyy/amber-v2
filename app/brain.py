"""The brain — a streamed token source, now driven by `agent_runtime`.

The contract is unchanged and deliberately so: ``think()`` returns an
``AsyncIterator[str]`` of text deltas, exactly as `app.responder.respond` does, so
the sentence splitter downstream can start TTS on the first sentence before the
whole response exists. Everything from the splitter onwards is untouched by this
module's rewrite.

What changed underneath is the loop. Amber used to drive the Anthropic Messages
API herself — stream a turn, spot ``tool_use``, run the tools, feed results back,
repeat — and that loop is the thing every agent in the ecosystem needs, so it now
lives in `agent_runtime` and Amber imports it. The `anthropic` SDK is gone from
this app entirely; the provider is OpenRouter, reached through the standard
OpenAI-compatible client.

Three consequences worth knowing:

* **Model choice is a named tier.** ``settings.llm_tier`` ("balanced") is resolved
  by `agent_runtime.model_router`, so upgrading the model every app uses is one
  edit in the router rather than one per app. Today "balanced" resolves to the
  Claude Haiku model Amber was already pinned to, so this is not a behaviour
  change — only an indirection.
* **Amber's tools are unchanged.** `AnthropicRegistryBroker` adapts the existing
  `app.tools` registry — the same ``get_tool_schemas()`` / ``run_tool()`` pair, the
  same functions — so a tool is still a plain Python call and Amber never makes an
  HTTP request to herself to add a task. Peers, when configured, are merged in
  alongside via `CompositeBroker`.
* **Tool use is still invisible downstream.** The runner may make several round
  trips before answering; only spoken text comes out of this iterator, and the
  caller's history is never polluted with tool plumbing.

The runner is rebuilt per turn rather than cached, because the broker binds this
turn's ``conversation_id`` and agent depth. That costs one small object; caching it
would mean either leaking one conversation's id into the next or threading mutable
state through a shared instance.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from agent_runtime import (
    AgentRunner,
    AnthropicRegistryBroker,
    CompositeBroker,
    MCPClient,
)
from agent_runtime import Settings as RuntimeSettings
# The peer record shape is agent-mcp-py's to define — it owns registration and
# resolution — so Amber parses her static peer list with its helper rather than
# inventing a second format that would drift.
from agent_mcp.registry import load_static_peers

from app.config import Settings, get_settings
from app.persona import SYSTEM_PROMPT
from app.tools import get_tool_schemas, run_tool

logger = logging.getLogger(__name__)


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
        default_tier=settings.llm_tier,
        max_tokens=settings.llm_max_tokens,
        max_steps=settings.max_tool_iterations,
        # Cost rows go in Amber's own database, beside her memory and the MCP
        # layer's tool log, so spend can be joined to what caused it.
        db_path=settings.memory_db_path,
        title="Amber",
    )


def build_broker(settings: Settings | None = None):
    """Assemble the tool broker for a turn, or ``None`` when tools are off.

    Amber's inline tools come first so that if a peer ever exposes a colliding
    name, hers wins — her own tools are the ones with no network in the way.
    """
    settings = settings or get_settings()
    if not settings.feature_tools:
        return None

    local = AnthropicRegistryBroker(get_tool_schemas, run_tool)

    peers = load_static_peers(settings.mcp_peers, settings.mcp_peer_token)
    if not peers:
        return local

    remote = MCPClient(list(peers), resolver=peers)
    return CompositeBroker([local, remote])


async def think(
    messages: list[dict],
    system: str | None = None,
    *,
    conversation_id: str | None = None,
) -> AsyncIterator[str]:
    """Stream Amber's reply for the given conversation history.

    ``messages`` is the running history as ``{"role", "content"}`` dicts — plain
    text on both sides, which is already what the OpenAI-compatible protocol wants,
    so no translation happens here. The system prompt is injected per turn rather
    than stored in the history; ``system`` is the persona prompt with the memory
    block appended (see `app.persona.compose_system_prompt`), falling back to the
    bare persona prompt.

    ``conversation_id`` is the session id. It ties this turn's model spend and tool
    calls together in the usage tables, and is forwarded to any peer agent Amber
    calls so a multi-agent exchange can be reconstructed afterwards.

    Yields text deltas as they arrive. Tool round trips happen inside and are
    invisible here.
    """
    settings = get_settings()
    system = system if system is not None else SYSTEM_PROMPT
    broker = build_broker(settings)

    runner = AgentRunner(
        model=settings.llm_tier,
        broker=broker,
        settings=runtime_settings(settings),
    )

    logger.debug(
        "LLM: %d message(s), tools=%s -> tier %s",
        len(messages),
        broker is not None,
        settings.llm_tier,
    )

    async for text in runner.stream(
        messages, system=system, conversation_id=conversation_id
    ):
        yield text
