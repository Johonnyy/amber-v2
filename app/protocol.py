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
"""

from __future__ import annotations

from typing import Any

# --- client -> server message types ---
INTERRUPT = "interrupt"  # stop speaking mid-response

# --- server -> client message types ---
READY = "ready"  # handshake accepted; server is listening
TRANSCRIPT = "transcript"  # what STT heard from the user
THINKING = "thinking"  # Amber is generating a response
AUDIO_CHUNK = "audio_chunk"  # metadata; the NEXT binary frame is this sentence
TURN_COMPLETE = "turn_complete"  # the full response has been sent
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


def error(message: str, code: str | None = None) -> dict[str, Any]:
    """Something went wrong. ``code`` (Phase 5) is an optional machine-readable
    tag (one of the ``ERR_*`` constants) so clients can react without parsing the
    human-readable ``message``."""
    frame: dict[str, Any] = {"type": ERROR, "message": message}
    if code is not None:
        frame["code"] = code
    return frame
