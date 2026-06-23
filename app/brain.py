"""The brain — Claude (Haiku) as a streamed token source.

This is the Phase-2 replacement for `app.responder`. It takes the per-connection
conversation history and streams Amber's reply back token by token, so the
pipeline's sentence splitter can start TTS on the first sentence before the whole
response exists. The contract is identical to `responder.respond`: an
``AsyncIterator[str]`` of text chunks. Everything downstream is unchanged.

Model, key, and token cap are all config-driven (`settings.llm_model`,
`settings.anthropic_api_key`, `settings.llm_max_tokens`) so the brain is swappable
without touching call sites.

No extended thinking is used: a voice loop is latency-sensitive and wants the
first spoken sentence out fast, so we stream a direct reply.

Phase 4 adds **tool use**. When tools are enabled (`settings.feature_tools`) the
brain runs the agentic loop: stream a turn, and if the model emits ``tool_use``,
execute the tool(s), feed the results back, and stream again — repeating until the
model answers in plain text or the iteration cap is hit. Any text the model speaks
along the way (e.g. "let me check that") streams through the same seam, so tool use
is invisible to everything downstream of the brain. The caller's history is never
mutated with tool plumbing — only the spoken text is recorded by the pipeline.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import lru_cache
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from app.config import Settings, get_settings
from app.persona import SYSTEM_PROMPT
from app.tools import get_tool_schemas, run_tool

if TYPE_CHECKING:
    from app.client_tools import ClientTools

logger = logging.getLogger(__name__)

# A tool dispatcher: (name, input) -> the string result fed back to the model.
ToolDispatch = Callable[[str, dict], Awaitable[str]]


@lru_cache
def get_client() -> AsyncAnthropic:
    """Process-wide Anthropic client (connection pool + key configured once)."""
    settings = get_settings()
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def think(
    messages: list[dict],
    system: str | None = None,
    *,
    client_tools: "ClientTools | None" = None,
) -> AsyncIterator[str]:
    """Stream Amber's reply for the given conversation history.

    ``messages`` is the Anthropic message list (alternating/​combinable
    user/assistant turns); the system prompt is injected here, not stored in the
    history. ``system`` is the full system prompt for this turn — Phase 3 passes
    the persona prompt with a memory block appended (see
    `app.persona.compose_system_prompt`); when omitted, the bare persona prompt is
    used so the Phase-2 contract is unchanged. Yields text deltas as they arrive.

    When tools are enabled the brain may make several LLM round trips to call tools
    before the final answer; all spoken text streams through in order. ``client_tools``
    (Phase 4+) is the connection's :class:`app.client_tools.ClientTools`: its
    declared tools are offered alongside Amber's own, and any ``client_*`` call is
    dispatched back over the WebSocket instead of through the server registry.
    """
    settings = get_settings()
    client = get_client()
    system = system if system is not None else SYSTEM_PROMPT

    tools: list[dict] = []
    if settings.feature_tools:
        tools += get_tool_schemas()
    if settings.feature_client_tools and client_tools is not None:
        tools += client_tools.schemas()

    dispatch = _make_dispatch(client_tools)

    if not tools:
        # No tools (flag off or none registered): the Phase-2/3 streaming path,
        # exactly as before — one stream, no tool plumbing.
        async for text in _stream_once(client, settings, system, messages):
            yield text
        return

    logger.debug("LLM: %d message(s), %d tool(s) -> %s", len(messages), len(tools), settings.llm_model)

    # Tool-use loop. Work on a copy so the caller's history is never polluted with
    # tool_use / tool_result blocks — the pipeline records only the spoken text.
    working: list[dict] = list(messages)
    for _ in range(settings.max_tool_iterations):
        final = None
        async with client.messages.stream(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            system=system,
            messages=working,
            tools=tools,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()

        if final.stop_reason != "tool_use":
            return

        # The model wants tools: record its turn (text + tool_use), run the calls,
        # and hand the results back for the next iteration.
        working.append({"role": "assistant", "content": final.content})
        working.append(
            {"role": "user", "content": await _run_tool_calls(final.content, dispatch)}
        )

    # Iteration cap hit while still calling tools. Force a final answer with tools
    # off, so the user always hears a reply built from whatever was gathered.
    logger.warning(
        "Tool loop hit max_tool_iterations=%d; forcing a final answer",
        settings.max_tool_iterations,
    )
    async for text in _stream_once(client, settings, system, working):
        yield text


async def _stream_once(
    client: AsyncAnthropic,
    settings: Settings,
    system: str,
    messages: list[dict],
) -> AsyncIterator[str]:
    """Stream a single turn with no tools — yields text deltas as they arrive."""
    async with client.messages.stream(
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        system=system,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


def _make_dispatch(client_tools: "ClientTools | None") -> ToolDispatch:
    """Build the per-turn tool dispatcher.

    Client-declared (``client_*``) tools are routed back to the connecting client;
    everything else goes through Amber's own process-wide registry. Both honor the
    "never raise into the brain" contract — failures come back as strings.
    """

    async def dispatch(name: str, tool_input: dict) -> str:
        if client_tools is not None and client_tools.handles(name):
            return await client_tools.call(name, tool_input)
        return await run_tool(name, tool_input)

    return dispatch


async def _run_tool_calls(content: list, dispatch: ToolDispatch) -> list[dict]:
    """Execute every ``tool_use`` block in an assistant turn, in order.

    Returns the matching ``tool_result`` blocks (one per call) as a single list,
    to be sent back as one user message. Tools never raise here — ``dispatch``
    converts failures into a result string the model can react to.
    """
    results: list[dict] = []
    for block in content:
        if getattr(block, "type", None) == "tool_use":
            logger.info("Tool call: %s(%s)", block.name, block.input)
            output = await dispatch(block.name, block.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )
    return results
