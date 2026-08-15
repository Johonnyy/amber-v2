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

Typed input adds one more *additive* client -> server frame, ``user_text``: a turn
whose text the client already has, so there is nothing to transcribe. It carries a
single ``text`` field and is the exact peer of a binary utterance — it takes a turn
slot, obeys the same rate limit and session cap, and barges in on an in-flight turn
the same way. Everything downstream is identical: the server still echoes a
``transcript`` frame (so a client renders both input modes through one path) and
still speaks the reply. Only the STT step is skipped. Clients that only send audio
are unaffected.

Voice control adds one *additive* frame in each direction, so a client can choose
how Amber sounds without an edit and a restart on the server:
  * ``set_voice`` (client -> server) — a **patch** over this connection's voice
    settings: any of ``voice``, ``model``, ``format``, ``speed``, ``instructions``.
    Absent keys are unchanged and unrecognised values are dropped silently; an
    explicit ``null`` resets that one field to the server's configured default.
  * ``voice`` (server -> client) — what is actually in effect, plus the catalogue
    of what this Amber accepts (so a picker needn't hardcode it). Sent once right
    after ``ready``, and again after every ``set_voice``. It is the acknowledgment:
    values are validated and clamped server-side, so this frame — never the patch —
    is the truth about what the next sentence will sound like.
A client that sends neither gets the install's configured voice, exactly as before.

Model selection adds one *additive* frame in each direction, the exact peer of the
voice pair above, so a client can choose *which brain answers* — and what a keyword
means — without an edit and a restart on the server:
  * ``set_model`` (client -> server) — two independent things, either or both:
    ``keyword`` picks the model for **this connection** (``null`` = back to the
    server's configured default), and ``map`` re-points keywords **for the whole
    install** (``{"coding": "vendor/model"}``, a ``null`` value resetting one to its
    built-in default). The map is applied first, so one frame can invent a keyword
    and select it.
  * ``model`` (server -> client) — what this connection resolves to, plus the whole
    keyword catalogue with each keyword's description and what it points at. Sent
    once right after ``voice``, and again after every ``set_model``. Like ``voice``,
    it is the acknowledgment: a value that failed validation is dropped silently and
    this frame — never the patch — says what took effect.
A client that sends neither uses ``AMBER_LLM_TIER``, exactly as before.

Turn-based conversations extend ``turn_complete`` *additively* with an optional
``awaiting_response`` field: ``True`` when Amber asked something it expects the user
to answer, so the client should keep the mic open and send the next utterance as a
continuation. The key is present only when ``True``; old clients ignore it and fall
back to one-shot turns. The field is per-turn, never sticky.
"""

from __future__ import annotations

from typing import Any

# --- client -> server message types ---
INTERRUPT = "interrupt"  # stop speaking mid-response
USER_TEXT = "user_text"  # a typed utterance, taken verbatim instead of transcribed
REGISTER_TOOLS = "register_tools"  # client declares tools Amber may call on it
TOOL_RESULT = "tool_result"  # the result of a client-side tool call (see TOOL_CALL)
SET_VOICE = "set_voice"  # patch how Amber sounds on this connection
SET_MODEL = "set_model"  # pick this connection's brain, and/or re-point a keyword

# --- server -> client message types ---
READY = "ready"  # handshake accepted; server is listening
TRANSCRIPT = "transcript"  # what STT heard from the user
THINKING = "thinking"  # Amber is generating a response
AUDIO_CHUNK = "audio_chunk"  # metadata; the NEXT binary frame is this sentence
TURN_COMPLETE = "turn_complete"  # the full response has been sent
MEMORY = "memory"  # what Amber currently remembers about the user (advisory)
TOOL_CALL = "tool_call"  # asks the client to run one of its declared tools
VOICE = "voice"  # the voice settings in effect, and what this Amber accepts
MODEL = "model"  # the brain in effect, and the keyword catalogue behind it
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


def turn_complete(sentences: int, awaiting_response: bool = False) -> dict[str, Any]:
    """The full response has been sent.

    ``awaiting_response`` (turn-based conversations) is ``True`` when Amber asked
    something it expects the user to answer, so the client should keep the mic open
    and treat the next utterance as a continuation. The key is attached only when
    ``True`` — the bare ``{"type", "sentences"}`` shape is preserved otherwise, so
    old clients (which ignore the unknown field anyway) degrade to one-shot turns.
    """
    frame: dict[str, Any] = {"type": TURN_COMPLETE, "sentences": sentences}
    if awaiting_response:
        frame["awaiting_response"] = True
    return frame


def memory(
    items: list[str], facts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The facts Amber is drawing on this turn, surfaced for the client to display.

    Advisory only: a client renders ``items`` (e.g. a memory panel) but the frame
    never affects the voice loop, so clients that ignore it are unaffected. ``items``
    is the same ranked set of distilled facts injected into the LLM's system prompt.

    ``facts`` is the same set again as ``{"id", "content", "tier"}`` records, and is
    attached only when supplied — ``items`` keeps its exact shape either way, so an
    existing client is untouched. The ids let a memory panel reference a specific
    fact (to show it, or to offer to correct it); the tier says whether Amber
    considers it settled knowledge or something still proving itself.
    """
    frame: dict[str, Any] = {"type": MEMORY, "items": list(items)}
    if facts:
        frame["facts"] = [dict(f) for f in facts]
    return frame


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


def voice(
    settings: dict[str, Any],
    options: dict[str, Any] | None = None,
    *,
    locked: bool = False,
) -> dict[str, Any]:
    """The voice settings in effect on this connection.

    ``settings`` is `app.voice.VoiceSettings.as_dict()` — the *effective* values
    after validation and clamping, which is why this frame rather than the client's
    own ``set_voice`` payload is the truth. ``options`` is the catalogue this Amber
    accepts (voices, models, containers, the speed range), so a client can build a
    picker from it instead of shipping a copy that drifts. ``locked`` is ``True``
    when ``AMBER_FEATURE_VOICE_CONTROL`` is off and ``set_voice`` is being ignored —
    a client should show the settings as read-only rather than let a user change
    something that will silently snap back.
    """
    frame: dict[str, Any] = {"type": VOICE, "settings": dict(settings)}
    if options is not None:
        frame["options"] = dict(options)
    if locked:
        frame["locked"] = True
    return frame


def model(
    settings: dict[str, Any],
    options: dict[str, Any] | None = None,
    *,
    locked: bool = False,
) -> dict[str, Any]:
    """Which brain this connection is talking to, and the catalogue behind it.

    ``settings`` is `app.models.state()` — the keyword in effect, the model id it
    resolves to *right now*, the server's own default keyword, and whether this
    connection chose one at all. ``options`` is `app.models.options()`: every keyword
    with its description and where it points, so a picker is built from what this
    Amber actually knows rather than a hardcoded list that drifts the moment a
    keyword is invented. ``locked`` is ``True`` when ``AMBER_FEATURE_MODEL_CONTROL``
    is off and ``set_model`` is being ignored — show the values, disable the controls.
    """
    frame: dict[str, Any] = {"type": MODEL, "settings": dict(settings)}
    if options is not None:
        frame["options"] = dict(options)
    if locked:
        frame["locked"] = True
    return frame


def error(message: str, code: str | None = None) -> dict[str, Any]:
    """Something went wrong. ``code`` (Phase 5) is an optional machine-readable
    tag (one of the ``ERR_*`` constants) so clients can react without parsing the
    human-readable ``message``."""
    frame: dict[str, Any] = {"type": ERROR, "message": message}
    if code is not None:
        frame["code"] = code
    return frame
