"""Tests for ``read_url`` and the HTML-to-text extractor.

The SSRF tests matter most. Amber runs on a VPS beside other services, and a tool
that fetches an arbitrary URL on request is a tool that can be talked into fetching
cloud instance credentials or a neighbour's admin port. Those tests assert not just
the refusal message but that **no HTTP client was ever constructed** — a guard that
runs after the request is out is not a guard.
"""

import httpx
import pytest

import app.tools.fetch as fetch
from app.config import Settings
from app.tools.htmltext import extract


def _settings(**over):
    base = dict(
        read_url_timeout_s=5.0,
        read_url_max_bytes=2 * 1024 * 1024,
        read_url_max_chars=4000,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


class _FakeResponse:
    def __init__(self, body=b"", status=200, content_type="text/html", encoding="utf-8"):
        self._body = body
        self.status_code = status
        self.headers = {"content-type": content_type} if content_type else {}
        self.encoding = encoding

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        # Chunked, so a reader that assumed one whole body would fail.
        for i in range(0, len(self._body), 8):
            yield self._body[i : i + 8]


class _FakeClient:
    def __init__(self, response, capture):
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        self._capture["url"] = url
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch(monkeypatch, response, capture=None, **over):
    capture = capture if capture is not None else {}
    monkeypatch.setattr(
        fetch.httpx, "AsyncClient", lambda *a, **k: _FakeClient(response, capture)
    )
    monkeypatch.setattr(fetch, "get_settings", lambda: _settings(**over))
    # Public hostnames resolve fine; the SSRF tests override this themselves.
    monkeypatch.setattr(fetch, "_blocked_address", lambda host: False)
    return capture


_PAGE = b"""
<html><head><title>  Match Report  </title>
<style>.a{color:red}</style></head>
<body>
  <nav><a href="/">Home</a><a href="/about">About</a></nav>
  <h1>Argentina win</h1>
  <p>Argentina won the 2022 final on penalties.</p>
  <script>console.log("tracking")</script>
  <footer>Copyright 2026</footer>
</body></html>
"""


# --- the happy path ---

async def test_read_url_returns_title_and_readable_text(monkeypatch):
    capture = _patch(monkeypatch, _FakeResponse(_PAGE))

    out = await fetch.read_url("https://example.com/report")

    assert capture["url"] == "https://example.com/report"
    assert out.startswith("Match Report — https://example.com/report")
    assert "Argentina won the 2022 final on penalties." in out
    # Chrome is stripped, not read back to the user as if it were content.
    assert "console.log" not in out
    assert "Copyright 2026" not in out
    assert "color:red" not in out


async def test_long_pages_are_truncated_and_say_so(monkeypatch):
    body = b"<html><body><p>" + b"word " * 5000 + b"</p></body></html>"
    _patch(monkeypatch, _FakeResponse(body), read_url_max_chars=200)

    out = await fetch.read_url("https://example.com/long")

    assert "[truncated" in out
    assert len(out) < 600


async def test_the_download_is_capped_while_streaming(monkeypatch):
    """Enforced on the stream rather than by trusting Content-Length, which can be
    missing or simply wrong."""
    body = b"<html><body><p>" + b"x" * 100_000 + b"</p></body></html>"
    _patch(monkeypatch, _FakeResponse(body), read_url_max_bytes=64)

    out = await fetch.read_url("https://example.com/huge")
    assert "example.com/huge" in out


# --- refusals ---

@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "https://something.local/",
        "https://db.internal/",
    ],
)
async def test_local_addresses_are_refused_without_any_request(monkeypatch, url):
    def boom(*a, **k):
        raise AssertionError("a blocked URL must never reach the network")

    monkeypatch.setattr(fetch.httpx, "AsyncClient", boom)
    monkeypatch.setattr(fetch, "get_settings", lambda: _settings())

    assert "local network" in await fetch.read_url(url)


async def test_a_hostname_resolving_to_a_private_address_is_refused(monkeypatch):
    """The realistic attack isn't a literal 127.0.0.1 — it's a public-looking name
    pointing at the cloud metadata endpoint."""
    def boom(*a, **k):
        raise AssertionError("a blocked URL must never reach the network")

    monkeypatch.setattr(fetch.httpx, "AsyncClient", boom)
    monkeypatch.setattr(fetch, "get_settings", lambda: _settings())
    monkeypatch.setattr(fetch, "_blocked_address", lambda host: host == "metadata.example")

    assert "local network" in await fetch.read_url("https://metadata.example/latest/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"])
async def test_non_web_schemes_are_refused(monkeypatch, url):
    def boom(*a, **k):
        raise AssertionError("a blocked URL must never reach the network")

    monkeypatch.setattr(fetch.httpx, "AsyncClient", boom)
    monkeypatch.setattr(fetch, "get_settings", lambda: _settings())

    assert "http and https" in await fetch.read_url(url)


async def test_an_empty_url_short_circuits(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no HTTP call for an empty url")

    monkeypatch.setattr(fetch.httpx, "AsyncClient", boom)
    monkeypatch.setattr(fetch, "get_settings", lambda: _settings())

    assert "Error" in await fetch.read_url("   ")


# --- failure modes ---

async def test_unreadable_content_types_are_named(monkeypatch):
    _patch(monkeypatch, _FakeResponse(b"%PDF-1.4", content_type="application/pdf"))
    out = await fetch.read_url("https://example.com/paper.pdf")
    assert "application/pdf" in out
    assert "can't read" in out


async def test_an_error_status_is_reported(monkeypatch):
    _patch(monkeypatch, _FakeResponse(b"", status=404))
    assert "404" in await fetch.read_url("https://example.com/missing")


async def test_a_timeout_is_reported_plainly(monkeypatch):
    _patch(monkeypatch, httpx.ReadTimeout("slow"))
    assert "took too long" in await fetch.read_url("https://example.com/slow")


async def test_a_transport_error_is_reported(monkeypatch):
    _patch(monkeypatch, httpx.ConnectError("boom"))
    assert "couldn't open" in await fetch.read_url("https://example.com/down")


async def test_a_page_with_no_text_says_so(monkeypatch):
    _patch(monkeypatch, _FakeResponse(b"<html><body><script>x=1</script></body></html>"))
    assert "couldn't find any readable text" in await fetch.read_url("https://example.com/js")


# --- the extractor, on its own ---

def test_extract_pulls_the_title_and_drops_chrome():
    title, text = extract(_PAGE.decode())
    assert title == "Match Report"
    assert "Argentina win" in text
    assert "console.log" not in text
    assert "Home" not in text


def test_extract_keeps_paragraph_boundaries():
    _, text = extract("<p>First para.</p><p>Second para.</p>")
    assert text == "First para.\n\nSecond para."


def test_extract_unescapes_entities():
    _, text = extract("<p>Tom &amp; Jerry &mdash; 5 &lt; 6</p>")
    assert "Tom & Jerry" in text
    assert "5 < 6" in text


def test_extract_collapses_html_whitespace():
    _, text = extract("<p>lots     of\n\n   space</p>")
    assert text == "lots of space"


def test_nested_chrome_does_not_readmit_content():
    """<nav> containing a <div> would end the skip on the first close tag if the
    parser tracked a flag instead of a depth."""
    _, text = extract("<nav><div>menu</div>more menu</nav><p>real content</p>")
    assert "menu" not in text
    assert text == "real content"


def test_extract_survives_malformed_markup():
    title, text = extract("<html><p>unclosed <b>bold <title>weird")
    assert isinstance(title, str)
    assert "unclosed" in text
