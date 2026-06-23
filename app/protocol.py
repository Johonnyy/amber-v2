"""WebSocket wire protocol — the stable public contract every client speaks.

Changing these shapes breaks every client, so treat them as versioned API.

Two frame kinds travel over the socket:

* **Binary frames** are raw audio.
  - client -> server: a complete recorded utterance (one user turn).
  - server -> client: one synthesized sentence of Amber's reply. Multiple binary
    frames arrive per turn, in order; the client plays them back to back. Each
    frame is preceded by an ``audio_chunk`` JSON frame describing it.

* **Text frames** are JSON control/metadata messages. Every JSON frame has a
  ``type`` field; see the constants below.

Phase 1 implements: ``ready``, ``transcript``, ``thinking``, ``audio_chunk``,
``turn_complete``, ``error`` (server -> client) and ``interrupt`` (client -> server).

Phase 5 extends two frames *additively* (old clients ignore the new fields, so the
contract stays compatible): ``ready`` now carries a ``session_id`` the client
echoes back as ``?session_id=`` to resume after a reconnect, and ``error`` may
carry a machine-readable ``code`` (see the ``ERR_*`` constants).

Memory surfacing (Phase 3) adds one more *additive* server -> client frame,
``memory``: the facts Amber is currently drawing on for this turn. It's advisory —
clients render it (e.g. a memory panel) but it never affects the voice loop — so a
client that ignores it behaves exactly as before. Sent at most once per turn,
before the reply streams.

Client-declared tools (Phase 4+) add three more *additive* frames so a client can
expose capabilities of its own device (show text, play a sound, ...) for Amber to
call:
  * ``register_tools`` (client -> server) — the client lists tools it can run.
    Each name is auto-prefixed with ``client_`` server-side.
  * ``tool_call`` (server -> client) — Amber asks the client to run one of those
    tools, carrying a correlation ``id``, the (prefixed) ``name``, and ``input``.
  * ``tool_result`` (client -> server) — the client returns the result for that
    ``id`` (with optional ``is_error``), which Amber feeds back to the model.
A client that never sends ``register_tools`` is unaffected — no ``tool_call`` is
ever sent to it.
"""

from __future__ import annotations

from typing import Any

# --- client -> server message types ---
INTERRUPT = "interrupt"  # stop speaking mid-response
REGISTER_TOOLS = "register_tools"  # client declares tools Amber may call on it
TOOL_RESULT = "tool_result"  # the result of a client-side tool call (see TOOL_CALL)

# --- server -> client message types ---
READY = "ready"  # handshake accepted; server is listening
TRANSCRIPT = "transcript"  # what STT heard from the user
THINKING = "thinking"  # Amber is generating a response
AUDIO_CHUNK = "audio_chunk"  # metadata; the NEXT binary frame is this sentence
TURN_COMPLETE = "turn_complete"  # the full response has been sent
MEMORY = "memory"  # what Amber currently remembers about the user (advisory)
TOOL_CALL = "tool_call"  # asks the client to run one of its declared tools
ERROR = "error"  # something went wrong this turn

# --- error codes (the optional ``code`` field on an error frame) ---
ERR_RATE_LIMITED = "rate_limited"  # too many utterances too fast; back off
ERR_PAYLOAD_TOO_LARGE = "payload_too_large"  # utterance exceeded max_audio_bytes
ERR_SESSION_LIMIT = "session_limit"  # session hit its lifetime turn cap
ERR_INTERNAL = "internal"  # an unexpected turn failure


def ready(session_id: str | None = None, resumed: bool = False) -> dict[str, Any]:
    """Handshake-accepted frame.

    ``session_id`` (Phase 5) is the id the client should store and present as
    ``?session_id=`` on a later reconnect to resume this conversation; ``resumed``
    is ``True`` when this connection picked up an existing session. Omitted when no
    id is supplied so the bare ``{"type": "ready"}`` shape is preserved.
    """
    frame: dict[str, Any] = {"type": READY}
    if session_id is not None:
        frame["session_id"] = session_id
        frame["resumed"] = resumed
    return frame


def transcript(text: str) -> dict[str, Any]:
    return {"type": TRANSCRIPT, "text": text}


def thinking(state: bool = True) -> dict[str, Any]:
    return {"type": THINKING, "active": state}


def audio_chunk(index: int, text: str, audio_format: str) -> dict[str, Any]:
    """Metadata for the binary audio frame that immediately follows.

    ``index`` is the 0-based sentence position within this turn, ``text`` is the
    sentence being spoken (handy for captions/debugging), ``audio_format`` is the
    container of the bytes (e.g. ``"mp3"``).
    """
    return {
        "type": AUDIO_CHUNK,
        "index": index,
        "text": text,
        "format": audio_format,
    }


def turn_complete(sentences: int) -> dict[str, Any]:
    return {"type": TURN_COMPLETE, "sentences": sentences}


def memory(items: list[str]) -> dict[str, Any]:
    """The facts Amber is drawing on this turn, surfaced for the client to display.

    Advisory only: a client renders ``items`` (e.g. a memory panel) but the frame
    never affects the voice loop, so clients that ignore it are unaffected. ``items``
    is the same ranked set of distilled facts injected into the LLM's system prompt.
    """
    return {"type": MEMORY, "items": list(items)}


def tool_call(call_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Ask the client to run one of its declared tools.

    ``call_id`` correlates this request with the ``tool_result`` the client sends
    back; ``name`` is the ``client_``-prefixed tool name; ``tool_input`` is the
    arguments object the model produced for the call.
    """
    return {
        "type": TOOL_CALL,
        "id": call_id,
        "name": name,
        "input": tool_input,
    }


def error(message: str, code: str | None = None) -> dict[str, Any]:
    """Something went wrong. ``code`` (Phase 5) is an optional machine-readable
    tag (one of the ``ERR_*`` constants) so clients can react without parsing the
    human-readable ``message``."""
    frame: dict[str, Any] = {"type": ERROR, "message": message}
    if code is not None:
        frame["code"] = code
    return frame
