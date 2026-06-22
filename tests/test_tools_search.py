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
    def __init__(self, data):
        self._data = data

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

    async def post(self, url, json=None, headers=None):
        self._capture["url"] = url
        self._capture["json"] = json
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
    assert "Python: A programming language." in out
    assert "snakes" in out
    assert capture["params"]["q"] == "python"


async def test_no_results_message(monkeypatch):
    _patch_httpx(monkeypatch, {"RelatedTopics": []}, {})
    monkeypatch.setattr(search, "get_settings", lambda: _settings())
    out = await search.web_search("asdfqwer")
    assert "No quick results" in out


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
    assert "42 is the answer." in out
    assert "Guide: Some content." in out
    assert capture["json"]["api_key"] == "key-123"


async def test_http_error_degrades_gracefully(monkeypatch):
    class _ErrClient(_FakeClient):
        async def get(self, url, params=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(search.httpx, "AsyncClient", lambda *a, **k: _ErrClient({}, {}))
    monkeypatch.setattr(search, "get_settings", lambda: _settings())

    out = await search.web_search("anything")
    assert "unavailable" in out.lower()
