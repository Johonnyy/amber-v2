"""Web search — fresh facts mid-conversation (Phase 4).

Three providers, selected by ``settings.search_provider``:

* ``anthropic`` (**default**) — Anthropic's *native server-side* web search tool.
  This is not an HTTP call we make: it's a server tool the model runs itself.
  Anthropic executes the search on its own infrastructure inside the LLM request
  and streams the answer back with citations, so it reliably handles live
  current-events queries (scores, prices, "what's happening now") that the
  keyless provider can't. See :func:`server_tool_schemas` — the brain folds the
  schema into its ``tools=[...]`` and never dispatches it (Anthropic does). The
  only credential it needs is the Anthropic key the brain already uses.
* ``tavily`` (needs ``AMBER_SEARCH_API_KEY``) — an LLM-oriented search API Amber
  calls itself, returning ranked snippets plus a synthesized answer. A
  self-dispatched fallback.
* ``duckduckgo`` (keyless) — DuckDuckGo's Instant Answer API. Returns only canned
  "instant answers" (no real web crawl), so it misses most current-events
  queries; kept as a no-key fallback.

The two self-dispatched providers are the *fast/local* kind of lookup — a single
HTTP call for a few snippets, **not** a research engine; anything heavy (read a
whole page, follow links, summarize a long document) belongs on the OpenClaw
bridge. Provider, key, result count, and timeout are all config-driven.

When the native provider is selected the inline ``web_search`` tool below is
hidden (the native server tool supersedes it and shares the name ``web_search``,
which the Anthropic API would otherwise reject as a duplicate).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _native_search_selected() -> bool:
    """True when the native Anthropic server-side web search tool is in use."""
    return get_settings().search_provider.lower() == "anthropic"


def _inline_search_available() -> bool:
    """The inline ``web_search`` tool is offered only for the self-dispatched
    providers — it's hidden when the native server tool replaces it (same name)."""
    return not _native_search_selected()


def server_tool_schemas() -> list[dict[str, Any]]:
    """Anthropic-executed *server* tool schemas to fold into ``tools=[...]``.

    Unlike registry tools (which the brain dispatches), a server tool runs on
    Anthropic's own infrastructure: the search executes inside the LLM request and
    its results stream back inline, so Amber never sees a ``tool_use`` to handle.
    Returns the native web search tool when ``search_provider == "anthropic"`` (the
    default), else an empty list (the self-dispatched providers register an inline
    tool instead).
    """
    settings = get_settings()
    if settings.search_provider.lower() != "anthropic":
        return []
    tool: dict[str, Any] = {
        "type": settings.search_tool_version,
        "name": "web_search",
    }
    if settings.search_max_uses:
        tool["max_uses"] = settings.search_max_uses
    return [tool]

_DDG_URL = "https://api.duckduckgo.com/"
_TAVILY_URL = "https://api.tavily.com/search"


async def _duckduckgo(query: str, settings: Settings) -> list[str]:
    params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    async with httpx.AsyncClient(timeout=settings.search_timeout_s) as client:
        resp = await client.get(_DDG_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    limit = settings.search_max_results
    parts: list[str] = []

    # A direct answer / abstract is the highest-signal field when present.
    if (answer := (data.get("Answer") or "").strip()):
        parts.append(answer)
    abstract = (data.get("AbstractText") or "").strip()
    if abstract:
        heading = (data.get("Heading") or "").strip()
        parts.append(f"{heading}: {abstract}" if heading else abstract)
    if (definition := (data.get("Definition") or "").strip()):
        parts.append(definition)

    for topic in data.get("RelatedTopics", []):
        if len(parts) >= limit:
            break
        text = topic.get("Text") if isinstance(topic, dict) else None
        if text and text.strip():
            parts.append(text.strip())

    return parts[:limit]


async def _tavily(query: str, settings: Settings) -> list[str]:
    payload = {
        "api_key": settings.search_api_key,
        "query": query,
        "max_results": settings.search_max_results,
        "include_answer": True,
    }
    async with httpx.AsyncClient(timeout=settings.search_timeout_s) as client:
        resp = await client.post(_TAVILY_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    parts: list[str] = []
    if (answer := (data.get("answer") or "").strip()):
        parts.append(answer)
    for r in data.get("results", [])[: settings.search_max_results]:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        if content:
            parts.append(f"{title}: {content}" if title else content)
    return parts


@registry.register(
    name="web_search",
    description=(
        "Search the web for a quick, fresh fact the user asks about — current "
        "events, prices, definitions, who/what/when lookups. Returns a few short "
        "snippets. Use it when your own knowledge may be stale or you're unsure; "
        "for heavier research or reading a specific page, delegate to OpenClaw."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query, phrased as you'd type it.",
            }
        },
        "required": ["query"],
    },
    available=_inline_search_available,
)
async def web_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "Error: empty search query."

    settings = get_settings()
    provider = settings.search_provider.lower()

    # The native provider is a server tool the model runs itself, so this inline
    # function is hidden from the model and unreachable via dispatch. Guard anyway.
    if provider == "anthropic":
        return "Native web search is handled by the model directly; no inline lookup."

    try:
        if provider == "tavily":
            if not settings.search_api_key:
                return (
                    "Web search isn't configured (the tavily provider needs "
                    "AMBER_SEARCH_API_KEY)."
                )
            results = await _tavily(query, settings)
        else:
            results = await _duckduckgo(query, settings)
    except httpx.HTTPError as exc:
        logger.warning("web_search failed: %s", exc)
        return f"Search is unavailable right now ({exc})."

    if not results:
        return f"No quick results for '{query}'."
    return "Search results:\n" + "\n".join(f"- {r}" for r in results)
