"""Web search — fresh facts mid-conversation.

Amber's training data has a cutoff, so anything current has to be looked up. This
is the fast/local kind of lookup: one HTTP call for an answer and a few sourced
snippets, **not** a research engine. Reading a whole page is `app.tools.fetch`;
long multi-step research belongs on a peer MCP server.

Provider selection (``AMBER_SEARCH_PROVIDER``):

* ``auto`` (**default**) — Tavily when ``AMBER_SEARCH_API_KEY`` is set, DuckDuckGo
  when it isn't. This is the honest default: the good backend when it's available,
  a working one when it isn't, and no configuration required either way.
* ``tavily`` — an LLM-oriented search API returning ranked snippets plus a
  synthesized answer. Needs a key; asking for it *explicitly* without one is a
  configuration error rather than a silent downgrade.
* ``duckduckgo`` — keyless. The Instant Answer API alone answers almost nothing
  ("what happened in the news today" returns an empty object), so this falls back
  to scraping DuckDuckGo's HTML results page. That scrape is best-effort and will
  break when their markup changes — it's the price of needing no key.

There used to be a third, ``anthropic``: a *native server-side* tool that ran inside
the LLM request on Anthropic's own infrastructure and streamed back cited results.
It went when the brain moved to `agent_runtime` — a server tool only exists inside a
provider's own request loop, and the OpenAI-compatible endpoint has no equivalent.
Tavily is the closest replacement, which is why ``auto`` prefers it.

**Results carry their URLs.** That's what makes `read_url` usable as a second hop
when a snippet isn't enough. The persona forbids reading a URL aloud.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.config import Settings, get_settings
from app.tools.registry import registry

logger = logging.getLogger(__name__)


_DDG_URL = "https://api.duckduckgo.com/"
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_TAVILY_URL = "https://api.tavily.com/search"

# How much of one snippet survives into the prompt. Enough to answer from, short
# enough that three of them don't dominate the turn.
_SNIPPET_CHARS = 400


def resolve_provider(settings: Settings) -> str:
    """Which backend this call should use.

    ``auto`` (and anything unrecognized) picks Tavily when a key exists and
    DuckDuckGo otherwise; an explicit choice is honoured as given.
    """
    provider = (settings.search_provider or "auto").strip().lower()
    if provider in ("tavily", "duckduckgo"):
        return provider
    return "tavily" if settings.search_api_key.strip() else "duckduckgo"


class Result:
    """One search hit: what it says, and where it came from."""

    __slots__ = ("title", "url", "snippet")

    def __init__(self, title: str = "", url: str = "", snippet: str = "") -> None:
        self.title = title.strip()
        self.url = url.strip()
        self.snippet = snippet.strip()[:_SNIPPET_CHARS]


def _format(query: str, answer: str, results: list[Result]) -> str:
    """The tool result the model reads.

    Answer first (it's usually the whole point), then numbered sources with their
    URLs so a follow-up ``read_url`` has something to work from.
    """
    if not answer and not results:
        return (
            f"No results for '{query}'. Try a shorter or differently-worded search; "
            "if it still comes back empty, say you couldn't find it rather than "
            "guessing."
        )

    parts: list[str] = []
    if answer:
        parts.append(f"Answer: {answer}")
    if results:
        parts.append("Sources:")
        for index, r in enumerate(results, start=1):
            head = f"{index}. {r.title}" if r.title else f"{index}."
            if r.url:
                head += f" — {r.url}"
            parts.append(head)
            if r.snippet:
                parts.append(f"   {r.snippet}")
    return "\n".join(parts)


async def _tavily(query: str, settings: Settings) -> tuple[str, list[Result]]:
    payload = {
        "api_key": settings.search_api_key,
        "query": query,
        "max_results": settings.search_max_results,
        "include_answer": True,
        "search_depth": "basic",
    }
    async with httpx.AsyncClient(timeout=settings.search_timeout_s) as client:
        resp = await client.post(
            _TAVILY_URL,
            json=payload,
            # Sent both ways on purpose: the body key is the documented form and
            # the header is what newer versions of the API expect.
            headers={"Authorization": f"Bearer {settings.search_api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    answer = (data.get("answer") or "").strip()
    results = [
        Result(r.get("title") or "", r.get("url") or "", r.get("content") or "")
        for r in (data.get("results") or [])[: settings.search_max_results]
    ]
    return answer, [r for r in results if r.snippet or r.url]


async def _duckduckgo(query: str, settings: Settings) -> tuple[str, list[Result]]:
    """Instant Answers first, then the HTML results page when it comes back empty.

    Instant Answers is a curated-answer API, not a web index — for most real
    questions it returns nothing at all, which is why the scrape exists.
    """
    limit = settings.search_max_results
    params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    async with httpx.AsyncClient(timeout=settings.search_timeout_s) as client:
        resp = await client.get(_DDG_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

        answer = (data.get("Answer") or "").strip()
        results: list[Result] = []

        abstract = (data.get("AbstractText") or "").strip()
        if abstract:
            results.append(
                Result(
                    (data.get("Heading") or "").strip(),
                    (data.get("AbstractURL") or "").strip(),
                    abstract,
                )
            )
        if not answer and (definition := (data.get("Definition") or "").strip()):
            results.append(Result("", (data.get("DefinitionURL") or "").strip(), definition))

        for topic in data.get("RelatedTopics", []):
            if len(results) >= limit:
                break
            if not isinstance(topic, dict):
                continue
            text = (topic.get("Text") or "").strip()
            if text:
                results.append(Result("", (topic.get("FirstURL") or "").strip(), text))

        if answer or results:
            return answer, results[:limit]

        # Nothing curated — scrape the real results page.
        return "", await _duckduckgo_html(query, limit, client)


# Two passes rather than one big pattern: the link and its snippet are separate
# elements, and an optional trailing group in a single non-greedy pattern silently
# matches empty every time. Anchors are found first, then each one's snippet is
# looked for in the markup between it and the next anchor.
_HTML_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<url>[^"]*)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_SNIPPET_RE = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(fragment: str | None) -> str:
    """Markup fragment to plain text."""
    return html.unescape(_TAG_RE.sub("", fragment or "")).strip()


def _unwrap(url: str) -> str:
    """DuckDuckGo wraps result links in a redirect; recover the real target."""
    if "duckduckgo.com/l/" not in url and not url.startswith("//duckduckgo.com/l/"):
        return url
    target = parse_qs(urlparse(url if "//" in url else f"https:{url}").query).get("uddg")
    return unquote(target[0]) if target else url


async def _duckduckgo_html(
    query: str, limit: int, client: httpx.AsyncClient
) -> list[Result]:
    """Scrape the HTML results page. Best-effort: markup changes will break it."""
    resp = await client.post(
        _DDG_HTML_URL,
        data={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (compatible; Amber/0.1)"},
    )
    resp.raise_for_status()

    page = resp.text
    links = list(_HTML_LINK_RE.finditer(page))

    results: list[Result] = []
    for index, match in enumerate(links):
        if len(results) >= limit:
            break
        title = _clean(match.group("title"))
        if not title:
            continue
        # The snippet belongs to this result if it appears before the next one.
        end = links[index + 1].start() if index + 1 < len(links) else len(page)
        snippet_match = _HTML_SNIPPET_RE.search(page, match.end(), end)
        snippet = _clean(snippet_match.group("snippet")) if snippet_match else ""
        results.append(Result(title, _unwrap(match.group("url") or ""), snippet))
    return results


@registry.register(
    name="web_search",
    description=(
        "Search the web. Use it for anything current or anything you're not sure "
        "about — news, prices, scores, schedules, releases, who currently holds a "
        "job, 'latest' anything — and prefer searching over answering from stale "
        "memory or hedging. Returns a short answer plus a few sourced snippets with "
        "their URLs. If a snippet isn't enough, follow up with read_url on the most "
        "promising one. For long multi-step research, hand the job to a peer agent "
        "instead."
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
    read_only=True,
)
async def web_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "Error: web_search needs something to search for."

    settings = get_settings()
    provider = resolve_provider(settings)

    try:
        if provider == "tavily":
            if not settings.search_api_key:
                return (
                    "Error: web search isn't configured (the tavily provider needs "
                    "AMBER_SEARCH_API_KEY). Tell the user search is unavailable."
                )
            answer, results = await _tavily(query, settings)
        else:
            answer, results = await _duckduckgo(query, settings)
    except httpx.HTTPError as exc:
        logger.warning("web_search failed: %s", exc)
        return (
            f"Error: search is unavailable right now ({exc}). Say so plainly rather "
            "than guessing at an answer."
        )

    return _format(query, answer, results)
