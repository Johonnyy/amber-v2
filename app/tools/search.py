"""Web search tool — a lightweight inline lookup (Phase 4).

This is the *fast/local* kind of search: a single HTTP call to fetch a few fresh
facts mid-conversation. It is deliberately **not** a research engine — anything
heavy (read this whole page, follow links, summarize a long document) belongs on
the OpenClaw bridge, not here.

Two providers, selected by ``settings.search_provider``:

* ``duckduckgo`` (default, keyless) — DuckDuckGo's Instant Answer API. Good for
  quick factual/entity lookups; returns an abstract, a direct answer, and a few
  related topics. No API key required, so search works out of the box.
* ``tavily`` (needs ``AMBER_SEARCH_API_KEY``) — an LLM-oriented search API that
  returns ranked result snippets plus a synthesized answer.

Provider, key, result count, and timeout are all config-driven.
"""

from __future__ import annotations

import logging

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
