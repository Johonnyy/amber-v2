"""Tests for the web_search tool — provider routing and response parsing.

No network: a fake ``httpx.AsyncClient`` returns canned JSON.
"""

import httpx
import pytest

import app.tools.search as search
from app.config import Settings


def _settings(**over):
    base = dict(search_provider="duckduckgo", search_max_results=3, search_timeout_s=5.0)
    base.update(over)
    return Settings(_env_file=None, **base)


class _FakeResponse:
    def __init__(self, data, text=""):
        self._data = data
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    """Stands in for httpx.AsyncClient; records the request and returns canned JSON."""

    def __init__(self, data, capture):
        self._data = data
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self._capture["url"] = url
        self._capture["params"] = params
        return _FakeResponse(self._data)

    async def post(self, url, json=None, headers=None, data=None):
        self._capture["url"] = url
        self._capture["json"] = json
        self._capture["data"] = data
        self._capture["headers"] = headers
        return _FakeResponse(self._data)


def _patch_httpx(monkeypatch, data, capture):
    monkeypatch.setattr(
        search.httpx, "AsyncClient", lambda *a, **k: _FakeClient(data, capture)
    )


async def test_duckduckgo_extracts_answer_and_topics(monkeypatch):
    capture = {}
    data = {
        "Heading": "Python",
        "AbstractText": "A programming language.",
        "Answer": "",
        "RelatedTopics": [{"Text": "Python (genus) of snakes"}],
    }
    _patch_httpx(monkeypatch, data, capture)
    monkeypatch.setattr(search, "get_settings", lambda: _settings())

    out = await search.web_search("python")
    assert "Python" in out
    assert "A programming language." in out
    assert "snakes" in out
    assert capture["params"]["q"] == "python"


async def test_results_carry_their_urls(monkeypatch):
    """The URL is what makes read_url usable as a second hop."""
    capture = {}
    data = {
        "Heading": "Python",
        "AbstractText": "A programming language.",
        "AbstractURL": "https://example.com/python",
        "RelatedTopics": [],
    }
    _patch_httpx(monkeypatch, data, capture)
    monkeypatch.setattr(search, "get_settings", lambda: _settings())

    out = await search.web_search("python")
    assert "https://example.com/python" in out
    assert "Sources:" in out


async def test_no_results_message_suggests_a_next_move(monkeypatch):
    """A dead-end result string is why a model retries the same broken call."""
    _patch_httpx(monkeypatch, {"RelatedTopics": []}, {})
    monkeypatch.setattr(search, "get_settings", lambda: _settings())
    out = await search.web_search("asdfqwer")
    assert "No results" in out
    assert "rather than guessing" in out


async def test_empty_query_short_circuits(monkeypatch):
    # Should not even build a client.
    def boom(*a, **k):
        raise AssertionError("no HTTP call for an empty query")

    monkeypatch.setattr(search.httpx, "AsyncClient", boom)
    monkeypatch.setattr(search, "get_settings", lambda: _settings())
    assert "Error" in await search.web_search("   ")


async def test_tavily_requires_key(monkeypatch):
    monkeypatch.setattr(
        search, "get_settings", lambda: _settings(search_provider="tavily")
    )
    out = await search.web_search("anything")
    assert "isn't configured" in out


async def test_tavily_parses_answer_and_results(monkeypatch):
    capture = {}
    data = {
        "answer": "42 is the answer.",
        "results": [{"title": "Guide", "content": "Some content."}],
    }
    _patch_httpx(monkeypatch, data, capture)
    monkeypatch.setattr(
        search,
        "get_settings",
        lambda: _settings(search_provider="tavily", search_api_key="key-123"),
    )

    out = await search.web_search("meaning of life")
    assert "Answer: 42 is the answer." in out
    assert "Guide" in out
    assert "Some content." in out
    # The key is sent both ways: the body form is documented, the header is what
    # newer versions of the API expect.
    assert capture["json"]["api_key"] == "key-123"
    assert capture["headers"]["Authorization"] == "Bearer key-123"


async def test_http_error_degrades_gracefully(monkeypatch):
    class _ErrClient(_FakeClient):
        async def get(self, url, params=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(search.httpx, "AsyncClient", lambda *a, **k: _ErrClient({}, {}))
    monkeypatch.setattr(search, "get_settings", lambda: _settings())

    out = await search.web_search("anything")
    assert "unavailable" in out.lower()


# --- native ("anthropic") server-side provider ---------------------------------

def test_the_native_server_side_provider_is_gone():
    """It was the default, and it was better: Anthropic ran the search inside the
    LLM request and streamed back cited results, which handled live queries the
    keyless API cannot. It went when the brain moved to `agent_runtime` — a server
    tool only exists inside a provider's own request loop, and the
    OpenAI-compatible endpoint has no equivalent. Recorded as a deliberate removal
    so nobody re-adds half of it: there is no schema to export and nothing for the
    model to run on our behalf.

    Set AMBER_SEARCH_API_KEY and select "tavily" for comparable quality.
    """
    assert not hasattr(search, "server_tool_schemas")
    assert not hasattr(search, "_inline_search_available")
    assert not hasattr(search, "_native_search_selected")

    from app.tools import get_tool_schemas
    import app.tools as tools_pkg

    assert not hasattr(tools_pkg, "get_server_tool_schemas")
    # web_search is always a real, dispatchable tool now — never hidden behind a
    # server tool that shares its name.
    assert "web_search" in {s["name"] for s in get_tool_schemas()}


async def test_an_unknown_provider_falls_back_to_the_keyless_one(monkeypatch):
    captured = {}

    async def fake_ddg(query, settings):
        captured["hit"] = True
        return "", [search.Result("Title", "https://example.com", "a result")]

    monkeypatch.setattr(search, "_duckduckgo", fake_ddg)
    monkeypatch.setattr(
        search, "get_settings", lambda: _settings(search_provider="anthropic")
    )
    out = await search.web_search("who won the world cup")
    assert captured.get("hit") is True
    assert "a result" in out


# --- provider resolution -------------------------------------------------------

def test_auto_picks_tavily_when_a_key_is_available():
    assert search.resolve_provider(
        _settings(search_provider="auto", search_api_key="k")
    ) == "tavily"


def test_auto_falls_back_to_the_keyless_provider_without_a_key():
    assert search.resolve_provider(
        _settings(search_provider="auto", search_api_key="")
    ) == "duckduckgo"


def test_an_explicit_provider_is_honoured():
    assert search.resolve_provider(_settings(search_provider="duckduckgo", search_api_key="k")) == "duckduckgo"
    assert search.resolve_provider(_settings(search_provider="tavily")) == "tavily"


# --- the keyless fallback ------------------------------------------------------

async def test_duckduckgo_scrapes_results_when_instant_answers_is_empty(monkeypatch):
    """Instant Answers returns nothing for most real questions, which is the whole
    reason the scrape exists — without it the keyless provider answers almost
    nothing."""
    capture = {}
    page = """
    <div class="result">
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fcup">
        Who won the World Cup
      </a>
      <a class="result__snippet">Argentina won in 2022.</a>
    </div>
    """

    class _Client(_FakeClient):
        async def get(self, url, params=None):
            capture["instant"] = True
            return _FakeResponse({"RelatedTopics": []})

        async def post(self, url, json=None, headers=None, data=None):
            capture["scraped"] = data
            return _FakeResponse({}, text=page)

    monkeypatch.setattr(search.httpx, "AsyncClient", lambda *a, **k: _Client({}, capture))
    monkeypatch.setattr(search, "get_settings", lambda: _settings())

    out = await search.web_search("who won the world cup")

    assert capture["scraped"] == {"q": "who won the world cup"}
    assert "Who won the World Cup" in out
    assert "Argentina won in 2022." in out
    # The redirect wrapper is unwrapped, so read_url gets a real target.
    assert "https://example.com/cup" in out
