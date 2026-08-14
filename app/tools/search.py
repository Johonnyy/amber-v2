"""Web search — fresh facts mid-conversation (Phase 4).

Two providers, selected by ``settings.search_provider``:

* ``duckduckgo`` (**default**, keyless) — DuckDuckGo's Instant Answer API. Returns
  only canned "instant answers" (no real web crawl), so it misses most
  current-events queries; kept because it needs no key.
* ``tavily`` (needs ``AMBER_SEARCH_API_KEY``) — an LLM-oriented search API Amber
  calls herself, returning ranked snippets plus a synthesized answer. The better
  answer whenever a key is available.

There used to be a third, ``anthropic``: a *native server-side* tool that ran
inside the LLM request on Anthropic's own infrastructure and streamed back cited
results, which was the default and handled live queries far better than either of
these. It went when the brain moved to `agent_runtime`. A server tool only exists
inside a provider's own request loop, and the OpenAI-compatible endpoint Amber now
speaks has no equivalent — so there is nothing to fold into ``tools=[...]`` and
nothing for the model to run on our behalf. If current-events quality matters, set
``AMBER_SEARCH_API_KEY`` and select ``tavily``.

Both providers are the *fast/local* kind of lookup — a single HTTP call for a few
snippets, **not** a research engine. Anything heavy (read a whole page, follow
links, summarize a long document) belongs on a peer MCP server. Provider, key,
result count, and timeout are all config-driven.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.tools.registry import registry

logger = logging.getLogger(__name__)


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
)
async def web_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "Error: empty search query."

    settings = get_settings()
    provider = settings.search_provider.lower()

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
