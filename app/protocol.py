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


def ready() -> dict[str, Any]:
    return {"type": READY}


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


def error(message: str) -> dict[str, Any]:
    return {"type": ERROR, "message": message}
