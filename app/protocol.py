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

Turn visibility adds two more *additive* server -> client frames, so a client can show
what a turn is actually doing instead of a spinner:

  * ``activity`` — one tool call, twice: ``phase="start"`` when it is dispatched and
    ``phase="end"`` when it returns, correlated by ``id``. Purely advisory, exactly
    like ``memory``: it never affects the voice loop and a client that ignores it
    behaves as before.

    It is emphatically **not** ``tool_call``. That frame is a *request* — Amber asking
    the client to run one of the client's own tools, blocking until a ``tool_result``
    comes back. ``activity`` is a *report* about work Amber is doing herself, and the
    client owes nothing in reply. Conflating the two would make a UI look like it had
    an unanswered obligation on every search.

    ``origin`` says which of the four brokers served the call, since the model sees
    one flat tool list: ``own`` (Amber's registry), ``client`` (this device's declared
    tools), ``signal`` (``expect_reply``, which does no work), or ``peer:<name>`` for a
    peer MCP server. A ``start`` is what makes a long call legible — a peer call may
    legitimately run for minutes — so a client should render on ``start`` and patch on
    ``end`` rather than waiting for a completed pair.

  * ``delta`` — raw reply text as the model produces it, before the sentence splitter
    sees it. ``audio_chunk`` already carries each spoken sentence, but sentences are
    the *speech* view: they arrive whole, and a client that concatenates them has to
    guess at the whitespace between, so newlines, headings, lists and code fences are
    gone by the time they reach a screen. ``delta`` is the *text* view of the same
    reply, and it is what makes rendering markdown possible.

    Both describe one reply, so a client renders **one or the other, never both** —
    prefer ``delta`` when any arrives and fall back to joining ``audio_chunk`` text
    otherwise, which is what keeps this additive for a client (or a server) that
    predates it.

    Two things about its pacing, because the obvious assumption is wrong. Synthesis
    applies backpressure all the way up: while a sentence is being spoken nothing
    pulls on the model stream, so text does **not** race ahead of speech. It leads by
    about one sentence — the one being synthesized — and then waits. This is a
    readable reply that keeps pace with the voice, not a fast document render.

    And on a barge-in, the last sentence's ``delta`` text is already sent while its
    audio never was, so a client holding both can mark anything past the final
    ``audio_chunk`` as written-but-unspoken. Amber's own history has always recorded
    what was *streamed* rather than what was *heard*, so this matches what she thinks
    she said.

**Everything above is a reply.** Every frame described so far answers something the
client did — an utterance, a control frame, a tool result. Two additions break that,
and they are the only frames in this protocol that Amber can originate.

``push`` (server -> client) is Amber saying something unprompted: a reminder whose time
arrived, a note the maintenance pass wrote, a build that finished somewhere else. It is
the frame reminders were waiting on — they have always been recorded, listable and
completable, and could never *fire*, because there was nowhere for a due reminder to go.

  Three things make it different from every advisory frame above.

  **It is durable, not best-effort.** ``memory``, ``activity``, ``delta`` and ``status``
  describe a turn in progress, so a client that misses one has missed nothing that
  outlives the turn. A reminder due at 17:30 while nothing is connected must still
  arrive, so a push is written to an outbox first and delivered when a client exists.
  Pending pushes are flushed on connect, right after the handshake.

  **Delivery is at-least-once and ``id`` is stable.** If the socket takes the frame and
  the process dies before the outbox is marked, the same ``id`` arrives again on the
  next connect. A client must therefore key on ``id`` and ignore one it has already
  seen, rather than assuming each frame is a new event. The alternative — at-most-once —
  would mean silently losing exactly the reminders that matter.

  **It never arrives mid-turn.** A push is held until the connection is idle. Not for
  politeness: ``audio_chunk`` promises that the *next* binary frame is its sentence, and
  a background task writing between those two sends would corrupt the audio stream for
  every client. Waiting for the turn to end removes that hazard entirely rather than
  narrowing it.

  ``kind`` says what sort of thing this is (``reminder``, ``reflection``, ``notice``,
  ``peer_event``) so a client can route it without parsing ``text``. ``ref`` carries the
  row it came from — ``{"reminder_id": 12}`` — which is what lets an acknowledgment act
  on the underlying thing rather than merely dismiss a card.

``push_ack`` (client -> server) closes that loop: ``{"id", "action"}`` where ``action``
is ``seen``, ``dismiss`` or ``complete``. ``complete`` on a reminder calls the *same*
store function the ``complete_reminder`` tool calls, so a reminder completed by tapping
and one completed by asking are the same row in the same state. A client that never
sends it still gets every push exactly as before; the outbox is marked delivered on a
successful send, not on the ack.

Confirmation adds the last pair, and it is the one frame here a client genuinely owes an
answer to. Amber has always been able to *describe* a risky action and never to ask
permission for one, which is why no tool anywhere in the ecosystem could be marked
``requires_confirmation``: the flag is enforced by `agent-mcp-py` against an
``X-Confirmed`` header, and Amber had no way to obtain the approval that header asserts.
Marking a tool would have made it permanently uncallable rather than safely gated.

  * ``confirm_request`` (server -> client) — Amber is about to run a tool that declares
    it needs human approval, and is blocked until an answer comes back. It carries the
    correlation ``id``, the ``name``, the ``origin`` (the same ``ORIGIN_*`` vocabulary
    ``activity`` uses, so a UI can say *Bloom wants to run a task* rather than printing a
    raw tool name) and the ``input`` the model produced.
  * ``confirm_response`` (client -> server) — ``{"id", "approved"}``.

  **It fails closed.** No client connected, no answer within the timeout, or an explicit
  denial are all the same outcome: the tool is not run and the model is told so, in a
  string it can react to within the turn. Silence is never approval — which is why this
  is a *request* like ``tool_call`` and emphatically not a report like ``activity``.

  Unlike ``push``, this one does arrive mid-turn, and that is safe for the same reason
  ``tool_call`` is: it is emitted from the task already driving the turn, which by
  construction is not between an ``audio_chunk`` and its bytes while it waits on a tool.

**How Amber is doing** adds a last trio, shaped exactly like ``memory_query`` /
``memory`` / ``memory_action`` because it is the same kind of thing: a browse, a
response, and a couple of verbs on what came back.

  * ``review_query`` (client -> server) — ``{topic, since?, limit?}``.
  * ``review`` (server -> client) — ``{topic, items, ack?}``.
  * ``review_action`` (client -> server) — ``{topic, action, id}``.

  ``topic`` is one of ``tools`` (per-tool success rate and p50/p95 latency),
  ``reflections`` (what the maintenance pass noticed about how conversations go, with
  ``promote`` and ``dismiss``) or ``evals`` (turns saved as regression cases, with
  ``archive``).

  Every one of these was already being recorded and shown to nobody: tool latency fed a
  single LLM prompt, the reflections table needed an MCP key to read, and
  ``reflections.dismissed`` had no writer at all — the self-improvement loop ran
  unattended and reported to no one.

  **Promoting a reflection is the interesting verb**, and it is what makes
  ``AMBER_FEATURE_SELF_NOTES`` safe to leave off: the note becomes an ordinary durable
  fact because *a person chose to keep it*, so you get the value of self-observation
  without the model editing its own instructions.

``eval_capture`` (client -> server) saves the turn you are looking at as a regression
case — the utterance, what should have been called, what was. It carries the whole case
rather than a pointer into the exchange log, because that table has no session id and
pairs user with assistant positionally, so a reference into it could quietly come to
mean a different conversation. Answered with a ``review`` frame for the ``evals`` topic.
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
MEMORY_ACTION = "memory_action"  # forget / restore / correct one remembered fact
MEMORY_QUERY = "memory_query"  # browse or search everything Amber remembers
PUSH_ACK = "push_ack"  # acknowledge (and optionally act on) a delivered push
CONFIRM_RESPONSE = "confirm_response"  # approve or deny a confirm_request
REVIEW_QUERY = "review_query"  # how is Amber doing? tools / reflections / evals
REVIEW_ACTION = "review_action"  # act on one reviewed item
EVAL_CAPTURE = "eval_capture"  # save this turn as a regression case

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
ACTIVITY = "activity"  # a tool call starting or finishing (advisory; see the docstring)
DELTA = "delta"  # raw reply text as generated, for rendering (advisory)
STATUS = "status"  # what this install can reach and what it has spent (advisory)
PUSH = "push"  # Amber, unprompted: a reminder fired, a note, something finished
CONFIRM_REQUEST = "confirm_request"  # may I run this? blocks until answered
REVIEW = "review"  # answers a review_query (advisory)
ERROR = "error"  # something went wrong this turn

# --- ``review.topic``: which "how is Amber doing" question this answers ---
REVIEW_TOOLS = "tools"  # per-tool success rate and latency
REVIEW_REFLECTIONS = "reflections"  # what the maintenance pass noticed about itself
REVIEW_EVALS = "evals"  # saved turns that went wrong, kept as regression cases

# --- ``push.kind``: what sort of unprompted thing this is ---
# A client routes on this rather than parsing ``text``, so a reminder can ring and a
# reflection can sit quietly in a panel without either having to be recognised by shape.
PUSH_REMINDER = "reminder"  # a reminder whose time arrived
PUSH_REFLECTION = "reflection"  # a note the maintenance pass wrote about how it's going
PUSH_NOTICE = "notice"  # generic, from another service via POST /push
PUSH_PEER_EVENT = "peer_event"  # something finished at a peer (a Bloom build, say)

# --- ``push_ack.action`` ---
ACK_SEEN = "seen"  # delivered and read; no state change
ACK_DISMISS = "dismiss"  # the user waved it away
ACK_COMPLETE = "complete"  # the user did the thing — acts on ``ref``

# --- the two phases of an ``activity`` frame ---
PHASE_START = "start"
PHASE_END = "end"

# --- ``activity.origin``: which broker served the call ---
# The model sees one flat tool list, so the origin has to be recovered from the name.
# `app.peers.classify_tool_name` is the one place that mapping lives.
ORIGIN_OWN = "own"  # Amber's own registry (app.tools)
ORIGIN_CLIENT = "client"  # a tool this device declared via ``register_tools``
ORIGIN_SIGNAL = "signal"  # ``expect_reply`` — a back-channel flag, not real work
ORIGIN_PEER_PREFIX = "peer:"  # a peer MCP server, e.g. ``peer:bloom``

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


def turn_complete(
    sentences: int, awaiting_response: bool = False, **stats: Any
) -> dict[str, Any]:
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
    # What the turn cost: ``steps``, ``tokens_in``, ``tokens_out``, ``cost_usd``,
    # ``model``. Attached only when there is something to say, so the canned path and
    # an install with cost tracking off still send the bare two-key frame. This is
    # newly *possible* rather than newly collected — the numbers always existed and
    # `AgentRunner.stream` discarded them.
    #
    # And where the turn's *time* went: ``timings`` (``total_ms`` always, plus
    # ``stt_ms`` / ``first_token_ms`` / ``tts_ms`` when that turn did those things)
    # and ``step_spans`` (each model call's start and end). Deliberately no
    # ``tools_ms`` — every tool call already arrives as an ``activity`` pair carrying
    # its own ``ms``, and measuring it a second time here would be two sources for
    # one number, with two chances to disagree. A client draws the waterfall from
    # both: the server's spans, and the tool calls it already has.
    frame.update(stats)
    return frame


def memory(
    items: list[str],
    facts: list[dict[str, Any]] | None = None,
    *,
    scope: str = "turn",
    ack: dict[str, Any] | None = None,
    total: int | None = None,
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

    ``scope`` says *why* this frame arrived, because one shape now answers two
    questions. ``"turn"`` is the original meaning — what Amber is drawing on right
    now, sent unprompted before the reply. ``"browse"`` answers a ``memory_query``
    and is everything she knows rather than what this turn needed, so a panel must
    not let the second quietly overwrite the first. ``total`` is how many active
    facts exist, so a browse can say what it is a slice of. ``ack`` settles a
    ``memory_action``: ``{"action", "id", "ok", "content"}``.

    All three are attached only when they apply, so the frame a client already parses
    is unchanged in the case it already handles.
    """
    frame: dict[str, Any] = {"type": MEMORY, "items": list(items)}
    if facts:
        frame["facts"] = [dict(f) for f in facts]
    if scope != "turn":
        frame["scope"] = scope
    if total is not None:
        frame["total"] = total
    if ack is not None:
        frame["ack"] = dict(ack)
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


def activity(
    call_id: str,
    phase: str,
    name: str,
    *,
    origin: str,
    tool_input: dict[str, Any] | None = None,
    result: str | None = None,
    ok: bool | None = None,
    ms: int | None = None,
    read_only: bool | None = None,
) -> dict[str, Any]:
    """One tool call, reported to the client as it starts and again as it ends.

    ``call_id`` correlates the pair — a client creates a row on ``start`` and patches
    it on ``end``, which is the whole point: a peer call can run for minutes, so a
    frame that only arrived on completion would leave the longest calls invisible for
    exactly as long as they were interesting.

    ``origin`` is one of the ``ORIGIN_*`` constants (``peer:<name>`` for peers). Only
    the fields that apply to this phase are attached, so a ``start`` frame carries no
    ``ok`` key rather than a null one — the same shape discipline the rest of this
    module keeps, and it lets a client treat "absent" as "not known yet".

    Advisory in the strict sense: nothing here feeds back into the turn, so dropping
    every one of these frames changes only what a screen shows.

    **An interrupted call gets no ``end``.** A barge-in cancels the turn while the
    call is still in flight, and the cancellation path is the one place a send is
    genuinely unsafe — it also runs when the *connection* is closing, and it sits
    directly between the user interrupting and Amber listening again. So a client
    should treat a call still open when ``turn_complete`` arrives as interrupted. It
    already knows: it sent the ``interrupt``.
    """
    frame: dict[str, Any] = {
        "type": ACTIVITY,
        "id": call_id,
        "phase": phase,
        "name": name,
        "origin": origin,
    }
    if tool_input is not None:
        frame["input"] = tool_input
    if read_only is not None:
        frame["read_only"] = read_only
    if result is not None:
        frame["result"] = result
    if ok is not None:
        frame["ok"] = ok
    if ms is not None:
        frame["ms"] = ms
    return frame


def status(**sections: Any) -> dict[str, Any]:
    """What this install can reach, what it is running, and what it has spent.

    The same discipline `voice` and `model` established: **the server says what is
    true rather than the client hardcoding it.** All of this already existed and was
    simply never sent — `peers.status()`, `peers.known_peers()`, `model_sync.status()`,
    the memory fact count, the lifespan's background tasks — reachable only from
    Python, so a UI could infer a peer's existence solely from a tool name it happened
    to see.

    Sections are passed through as given so this stays a transport rather than a
    schema to keep in step with six other modules. Advisory, like `memory` and
    `activity`: a client that ignores it loses a panel, not a turn.

    **Never put a peer's token in here.** `PeerRecord` carries one, and the shape of
    this frame invites handing the whole record over.
    """
    return {"type": STATUS, **sections}


def delta(text: str) -> dict[str, Any]:
    """Raw reply text, as generated, before the sentence splitter sees it.

    The text peer of ``audio_chunk``: same reply, but with the model's own whitespace
    intact, which is what a client needs to render markdown. Render one or the other,
    never both — see the module docstring.
    """
    return {"type": DELTA, "text": text}


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


def push(
    push_id: str,
    kind: str,
    text: str,
    *,
    created_at: str | None = None,
    ref: dict[str, Any] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Amber, unprompted — the only server-originated frame in this protocol.

    ``push_id`` is stable across redeliveries, which is the whole contract: delivery is
    at-least-once, so a client keys on it and ignores a repeat. ``kind`` is one of the
    ``PUSH_*`` constants. ``ref`` points at the row this came from (``{"reminder_id":
    12}``), so a ``push_ack`` can act on the underlying thing instead of only dismissing
    a card.

    Never sent while a turn is in flight — see the module docstring for why that is a
    correctness rule about the audio stream and not a matter of taste.
    """
    frame: dict[str, Any] = {"type": PUSH, "id": push_id, "kind": kind, "text": text}
    if title:
        frame["title"] = title
    if created_at:
        frame["created_at"] = created_at
    if ref:
        frame["ref"] = dict(ref)
    return frame


def confirm_request(
    call_id: str,
    name: str,
    *,
    origin: str,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the human whether Amber may run this tool. Blocks the turn until answered.

    A *request*, like ``tool_call`` and unlike ``activity`` — the client owes exactly one
    ``confirm_response`` carrying this ``call_id``. If none arrives before the timeout the
    call is refused, because silence must never read as approval.

    ``origin`` is the ``ORIGIN_*`` vocabulary ``activity`` already uses, so a UI can name
    who is asking rather than printing a prefixed tool name at someone.
    """
    frame: dict[str, Any] = {
        "type": CONFIRM_REQUEST,
        "id": call_id,
        "name": name,
        "origin": origin,
    }
    if tool_input is not None:
        frame["input"] = tool_input
    return frame


def review(
    topic: str, items: list[dict[str, Any]], *, ack: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    """How Amber is doing, by topic. Answers a ``review_query``.

    One frame for three panels rather than three frames, and the shape is deliberately
    the trio `memory_query` / `memory` / `memory_action` established: a browse, a
    response, and a small set of actions on what came back. Tool reliability, the
    maintenance pass's self-review notes and saved eval cases are all the same kind of
    thing — a read-only look at how the system is behaving, with one or two verbs.

    All of this is data Amber has always had and never showed anyone.
    `store.event_summary` fed exactly one LLM prompt; the reflections table was
    reachable only with an MCP key; eval cases had nowhere to live at all.

    ``ack`` settles a ``review_action`` the same way `memory`'s does, so a panel that
    clicked something knows whether it took.
    """
    frame: dict[str, Any] = {"type": REVIEW, "topic": topic, "items": list(items)}
    if ack is not None:
        frame["ack"] = dict(ack)
    frame.update(extra)
    return frame


def error(message: str, code: str | None = None) -> dict[str, Any]:
    """Something went wrong. ``code`` (Phase 5) is an optional machine-readable
    tag (one of the ``ERR_*`` constants) so clients can react without parsing the
    human-readable ``message``."""
    frame: dict[str, Any] = {"type": ERROR, "message": message}
    if code is not None:
        frame["code"] = code
    return frame
