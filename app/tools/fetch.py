"""``read_url`` — read one web page, when a search snippet isn't enough.

Search gives Amber a sentence or two per source. Often that's the answer; often it
isn't, and the difference between "I found something about it" and an actual answer
is being able to open the page. This is the second hop: `web_search` returns URLs
precisely so this tool has something to follow.

**This is the one genuinely new attack surface in Amber.** She runs on a VPS beside
other services, and a tool that fetches an arbitrary URL on request is a tool that
can be talked into fetching `http://169.254.169.254/` (cloud instance credentials)
or a neighbouring service's admin port. `_check_url` refuses non-HTTP schemes and
any host that resolves into a loopback, private, link-local, or otherwise reserved
range — and it refuses **before** an HTTP client is constructed, so a blocked
request makes no network call at all.

The residual gap is DNS rebinding: a hostname that passes the check and then
resolves to something else on the connection itself. Closing it properly means
pinning the resolved address through the socket, which httpx doesn't expose
cleanly. It's documented rather than papered over — the realistic attack (a model
persuaded to fetch a literal metadata IP or an internal hostname) is closed.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.tools.htmltext import extract
from app.tools.registry import registry

logger = logging.getLogger(__name__)

_READABLE_TYPES = ("text/html", "application/xhtml", "text/plain", "application/json")
_USER_AGENT = "Mozilla/5.0 (compatible; Amber/0.1; personal assistant)"

# Hostnames that never route anywhere legitimate from Amber's perspective.
_BLOCKED_NAMES = ("localhost", "localhost.localdomain")
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


def _blocked_address(host: str) -> bool:
    """True if ``host`` is (or resolves to) an address Amber must not reach."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable: let the request fail normally rather than claiming it's
        # dangerous. Nothing is reachable, so nothing is at risk.
        return False

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return True
    return False


def _check_url(url: str) -> str | None:
    """The reason this URL can't be fetched, or ``None`` if it's allowed."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "Error: that isn't a valid web address."

    if parsed.scheme not in ("http", "https"):
        return "Error: I can only read http and https pages."
    host = (parsed.hostname or "").lower()
    if not host:
        return "Error: that isn't a valid web address."
    if host in _BLOCKED_NAMES or host.endswith(_BLOCKED_SUFFIXES):
        return "Error: I can't read pages on the local network."
    if _blocked_address(host):
        return "Error: I can't read pages on the local network."
    return None


@registry.register(
    name="read_url",
    description=(
        "Read the main text of a web page. Use it when a web_search snippet isn't "
        "enough to answer properly, or when the user names a specific page or gives "
        "you a link. Don't guess or invent URLs — use one from a search result or "
        "from the user. Returns the page title and its readable text, truncated if "
        "it's long."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full http or https URL of the page to read.",
            }
        },
        "required": ["url"],
    },
    read_only=True,
)
async def read_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "Error: read_url needs a web address."

    # Checked before a client exists, so a refused URL is never fetched at all.
    refusal = _check_url(url)
    if refusal:
        logger.warning("read_url refused: %s", url)
        return refusal

    settings = get_settings()
    max_bytes = settings.read_url_max_bytes

    try:
        async with httpx.AsyncClient(
            timeout=settings.read_url_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return (
                        f"Error: that page returned {resp.status_code}. Tell the "
                        "user the page couldn't be opened."
                    )

                content_type = (resp.headers.get("content-type") or "").lower()
                if content_type and not content_type.startswith(_READABLE_TYPES):
                    kind = content_type.split(";")[0].strip() or "that file type"
                    return f"Error: that link is {kind}, which I can't read."

                # Stream with a hard cap rather than trusting Content-Length: a
                # missing or lying header shouldn't be able to pull an unbounded
                # body into memory.
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if max_bytes > 0 and size >= max_bytes:
                        break
                body = b"".join(chunks).decode(
                    resp.encoding or "utf-8", errors="replace"
                )
    except httpx.TimeoutException:
        return "Error: that page took too long to load."
    except httpx.HTTPError as exc:
        logger.warning("read_url failed for %s: %s", url, exc)
        return f"Error: I couldn't open that page ({exc})."

    title, text = extract(body)
    if not text:
        return "Error: I opened that page but couldn't find any readable text on it."

    limit = settings.read_url_max_chars
    truncated = limit > 0 and len(text) > limit
    if truncated:
        text = text[:limit]

    header = f"{title} — {url}" if title else url
    suffix = "\n\n[truncated — this is the start of the page]" if truncated else ""
    return f"{header}\n\n{text}{suffix}"
