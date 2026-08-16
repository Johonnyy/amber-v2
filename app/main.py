"""FastAPI application + the WebSocket voice endpoint.

This is the thin transport layer. All the real work lives in `app.pipeline`; here
we accept the socket, authenticate it, attach a session, route frames, wire
interruption, and enforce the cost/abuse guardrails.

Every client speaks the protocol in `app.protocol`:
  * binary frame in  = a recorded utterance -> triggers a turn
  * ``{"type": "interrupt"}`` in = stop speaking the current turn
  * binary while already speaking = barge-in (cancel current turn, start the new one)

Phase 5 additions:
  * **Auth** — when ``AMBER_AUTH_SECRET`` is set, a client must present it as
    ``?token=`` or an ``Authorization: Bearer`` header, or the socket is refused.
  * **Sessions** — each connection gets a session id (sent in ``ready``). A client
    that reconnects with ``?session_id=`` resumes its retained history.
  * **Guardrails** — oversized utterances, per-session rate limits, and a lifetime
    turn cap are rejected with a coded ``error`` frame before any spend.
  * **Error recovery** — a single failed turn becomes an ``error`` frame, never a
    dropped connection; logs are tagged with the session id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status

from app import memory_control, model_sync, models, peers, protocol, signals
# Aliased: `fastapi.status` is already imported above for the WS close codes, and
# these two would otherwise be one name meaning two things in the same module.
from app import status as status_report
from app.config import Settings, get_settings
from app.pipeline import run_turn
from app.session import Session, SessionManager, get_session_manager
from app.voice import options as voice_options

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("amber")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background work that runs alongside the voice socket.

    Four things, all optional and all independent of the voice loop:

    * the **signal writer**, which batches telemetry to SQLite off the turn path;
    * the **maintenance loop**, which curates memory and writes self-review notes;
    * the two **sync-store pulls** — the shared model-keyword table, and the peer
      list that decides which other agents Amber can call at all;
    * **Amber's own MCP server** — a mounted sub-app's lifespan never runs, so the
      host has to enter the session manager itself or the first MCP request fails.
      `agent_mcp` wraps that (plus sync-store registration and its heartbeat).

    With everything disabled this is an empty context and `/ws` behaves exactly as
    before.
    """
    settings = get_settings()
    background: list[asyncio.Task] = []

    if settings.feature_signals:
        from app import signals

        background.append(asyncio.create_task(signals.drain_loop()))
    if settings.feature_memory and settings.feature_maintenance:
        from app.maintenance import maintenance_loop

        background.append(asyncio.create_task(maintenance_loop(settings)))
    if model_sync.sync_enabled(settings):
        # The ecosystem's shared keyword table. Reconciles immediately, so a keyword
        # re-pointed elsewhere while this box was down is in effect for the first
        # turn after a restart rather than five minutes into it.
        background.append(asyncio.create_task(model_sync.sync_loop(settings)))
    if peers.discovery_enabled(settings):
        # Peer discovery. Same shape and the same reason as the keyword sync above:
        # a peer connected from Aperture while this box was restarting should be
        # callable on the first turn after it comes back, not five minutes into it.
        #
        # Without this the only peers Amber can reach are the ones spelled out in
        # AMBER_MCP_PEERS — and an unlisted peer produces no error of any kind,
        # because it is not a failing tool, it is no tool. See app/peers.py.
        background.append(asyncio.create_task(peers.refresh_loop(settings)))

    try:
        if not settings.mcp_server_enabled:
            logger.info("MCP server disabled (no AMBER_MCP_KEYS); serving voice only")
            yield
        else:
            from app.mcp_server import get_mcp_server

            async with get_mcp_server().lifespan():
                logger.info("MCP server mounted at /mcp")
                yield
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


app = FastAPI(title="Amber", version="0.1.0", lifespan=lifespan)


def _mount_mcp(app: FastAPI, settings: Settings) -> None:
    """Attach Amber's MCP server and its usage endpoint, if enabled.

    Routes rather than ``app.mount``: ``Mount("/mcp")`` alone does not match a bare
    ``POST /mcp``, so the host router answers it with a 307 that the MCP client does
    not follow. ``routes()`` claims both forms.
    """
    if not settings.mcp_server_enabled:
        return
    from app.mcp_server import get_mcp_server

    mcp = get_mcp_server()
    app.router.routes.extend(mcp.routes())
    app.router.routes.extend(mcp.usage_routes())


_mount_mcp(app, settings)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for systemd / load balancers."""
    return {"status": "ok", "service": "amber", "version": app.version}


def _authorized(websocket: WebSocket, settings: Settings) -> bool:
    """True if the socket may connect. Open when no secret is configured; otherwise
    the secret must arrive as ``?token=`` or ``Authorization: Bearer <secret>``."""
    if not settings.auth_enabled:
        return True
    token = websocket.query_params.get("token", "")
    if not token:
        header = websocket.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    return token == settings.auth_secret


@app.websocket("/ws")
async def voice_socket(websocket: WebSocket) -> None:
    settings = get_settings()
    if not _authorized(websocket, settings):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("Rejected WS connection: bad/missing token")
        return

    await websocket.accept()

    manager = get_session_manager()
    requested = websocket.query_params.get("session_id") or None
    session, resumed = manager.resume_or_create(requested)
    await websocket.send_json(protocol.ready(session.id, resumed))
    # Right after ready, so a client knows how Amber currently sounds and what it may
    # ask for without hardcoding the catalogue. Purely additive — a client that
    # doesn't know the frame ignores it.
    await websocket.send_json(
        protocol.voice(
            session.voice.as_dict(),
            voice_options(),
            locked=not settings.feature_voice_control,
        )
    )
    # And which brain is answering, on the same terms — sent unconditionally so a
    # client can show the model without asking, and additive so one that doesn't know
    # the frame ignores it.
    await websocket.send_json(_model_frame(session, settings))
    # And what this install can actually reach — peers, sync, which halves of Amber
    # are switched on. Same terms again: the server says what is true so a client
    # doesn't infer it from tool names it happens to see go past.
    await websocket.send_json(protocol.status(**await status_report.build(session, settings)))
    logger.info(
        "[%s] Client %s (%d turn(s) of history)",
        session.id,
        "resumed" if resumed else "connected",
        len(session.conversation.messages),
    )

    async def send_json(payload: dict) -> None:
        await websocket.send_json(payload)

    async def send_bytes(data: bytes) -> None:
        await websocket.send_bytes(data)

    # Attach this connection's send channel so client-declared tools can be called
    # back over the socket. Detached again in the finally below.
    session.client_tools.bind(send_json)

    current_turn: asyncio.Task | None = None

    async def cancel_current(reason: str) -> None:
        nonlocal current_turn
        if current_turn and not current_turn.done():
            logger.info("[%s] Interrupting current turn (%s)", session.id, reason)
            # An interrupted reply usually means it was too long, or wrong. Worth
            # noticing over time, so the maintenance pass can say so.
            signals.record(
                signals.KIND_BARGE_IN, name=reason, session_id=session.id
            )
            current_turn.cancel()
            try:
                await current_turn
            except asyncio.CancelledError:
                pass
        current_turn = None

    async def start_turn(
        *, audio: bytes | None = None, text: str | None = None
    ) -> None:
        """Spawn one turn, spoken or typed. The caller has already admitted it.

        Both input modes share this: barge-in on any in-flight turn, count the turn
        against the session, then run it off the receive loop so the socket keeps
        reading (that's what makes interrupt work).
        """
        nonlocal current_turn
        await cancel_current("barge-in")
        session.turns += 1
        manager.touch(session)
        current_turn = asyncio.create_task(
            _guarded_turn(audio, send_json, send_bytes, session, text=text)
        )

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is not None:
                if not await _admit_utterance(data, session, settings, send_json):
                    continue
                await start_turn(audio=data)
                continue

            text = message.get("text")
            if text is not None:
                await _handle_control(
                    text, cancel_current, start_turn, session, settings, send_json
                )

    except WebSocketDisconnect:
        logger.info("[%s] Client disconnected", session.id)
    finally:
        await cancel_current("connection closing")
        # Detach the send channel and fail any in-flight client tool calls; the
        # declared tool specs are kept so a reconnect with this id still has them.
        session.client_tools.unbind()
        # Don't drop the session: keep it warm so a reconnect with this id resumes.
        # The manager's TTL reclaims it if the client never comes back.
        manager.touch(session)
        logger.info("[%s] Session detached (retained for resume)", session.id)


def _model_frame(session: Session, settings: Settings) -> dict:
    """The ``model`` frame for this connection, catalogue included.

    Built in one place because it goes out on the handshake and again after every
    ``set_model``, and the two must agree — a client comparing them to decide whether
    its request took effect is the whole acknowledgment mechanism.
    """
    return protocol.model(
        models.state(session.model_keyword, settings),
        models.options(settings),
        locked=not settings.feature_model_control,
    )


async def _admit_utterance(
    data: bytes, session: Session, settings: Settings, send_json
) -> bool:
    """Apply the cost/abuse guardrails to an inbound *spoken* utterance.

    Returns ``True`` if the turn may proceed; otherwise sends a coded ``error``
    frame and returns ``False``. Checks run cheapest-first so a rejected utterance
    spends nothing on STT/LLM/TTS. The size check is audio-specific; the rest are
    shared with typed turns via ``_admit_turn``.
    """
    if settings.max_audio_bytes > 0 and len(data) > settings.max_audio_bytes:
        logger.warning(
            "[%s] Rejected oversize utterance: %d > %d bytes",
            session.id,
            len(data),
            settings.max_audio_bytes,
        )
        await send_json(
            protocol.error("That audio is too large.", protocol.ERR_PAYLOAD_TOO_LARGE)
        )
        return False

    return await _admit_turn(session, settings, send_json)


async def _admit_turn(session: Session, settings: Settings, send_json) -> bool:
    """The guardrails every turn obeys, however the user produced it.

    A typed turn costs the same LLM/TTS spend as a spoken one, so the session cap
    and the rate limit apply identically — only the audio size check doesn't.
    """
    if (
        settings.max_turns_per_session > 0
        and session.turns >= settings.max_turns_per_session
    ):
        logger.warning("[%s] Session lifetime turn cap reached", session.id)
        await send_json(
            protocol.error(
                "This session has reached its limit. Reconnect to continue.",
                protocol.ERR_SESSION_LIMIT,
            )
        )
        return False

    if not session.limiter.allow():
        logger.warning("[%s] Rate limited", session.id)
        await send_json(
            protocol.error(
                "You're going a bit fast — give me a moment.",
                protocol.ERR_RATE_LIMITED,
            )
        )
        return False

    return True


async def _handle_control(
    text: str,
    cancel_current,
    start_turn,
    session: Session,
    settings: Settings,
    send_json,
) -> None:
    """Handle an inbound JSON control frame."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(
            "[%s] Ignoring non-JSON text frame: %r", session.id, text[:120]
        )
        return

    kind = payload.get("type")
    if kind == protocol.INTERRUPT:
        await cancel_current("client interrupt")
    elif kind == protocol.USER_TEXT:
        # A typed turn: the client already has the words, so STT is skipped. It is
        # otherwise a peer of a spoken utterance — same guardrails, same barge-in.
        typed = payload.get("text")
        if not isinstance(typed, str) or not typed.strip():
            # Blank/malformed, like any other bad control frame: ignore quietly
            # rather than spend a turn or an error frame on it.
            logger.debug("[%s] Ignoring empty user_text frame", session.id)
        elif await _admit_turn(session, settings, send_json):
            await start_turn(text=typed.strip())
    elif kind == protocol.REGISTER_TOOLS:
        names = session.client_tools.register(payload.get("tools"))
        logger.info("[%s] Client registered %d tool(s): %s", session.id, len(names), names)
    elif kind == protocol.SET_VOICE:
        # A patch over this connection's voice. Validated and clamped in
        # `app.voice`; the ack below reports what actually took effect, so a client
        # never has to guess whether its value survived.
        if settings.feature_voice_control:
            session.voice = session.voice.patched(payload)
            logger.info("[%s] Voice set: %s", session.id, session.voice.as_dict())
        else:
            logger.debug("[%s] Ignoring set_voice (voice control disabled)", session.id)
        await send_json(
            protocol.voice(
                session.voice.as_dict(),
                voice_options(),
                locked=not settings.feature_voice_control,
            )
        )
    elif kind == protocol.SET_MODEL:
        # Two independent things in one frame: which keyword *this* connection uses,
        # and what the keywords mean for the whole install. The map is applied first
        # so a single frame can invent a keyword and immediately select it.
        if settings.feature_model_control:
            if "map" in payload:
                # A SQLite write; off the event loop, like every other DB touch here.
                changed = await asyncio.to_thread(models.apply_map, payload["map"], settings)
                if changed:
                    logger.info("[%s] Re-pointed keyword(s): %s", session.id, changed)
                    # Already in effect locally; this only tells the rest of the
                    # ecosystem. Fire-and-forget so a slow store can't stall the ack.
                    model_sync.schedule(settings)
            if "keyword" in payload:
                session.model_keyword = _chosen_keyword(payload["keyword"])
                logger.info(
                    "[%s] Model set: %s",
                    session.id,
                    session.model_keyword or "(server default)",
                )
        else:
            logger.debug("[%s] Ignoring set_model (model control disabled)", session.id)
        await send_json(_model_frame(session, settings))
    elif kind == protocol.TOOL_RESULT:
        # A result for a client-side tool call the brain is awaiting.
        session.client_tools.resolve(
            payload.get("id"),
            payload.get("content", ""),
            bool(payload.get("is_error")),
        )
    elif kind in (protocol.MEMORY_ACTION, protocol.MEMORY_QUERY):
        # Curating memory from the UI, landing on the same store functions Amber's
        # own `forget_fact` / `correct_fact` tools call. Two ways in, one code path —
        # a fact forgotten by asking and one forgotten by clicking are the same row
        # in the same state.
        if not memory_control.enabled(settings):
            logger.debug("[%s] Ignoring %s (memory control off)", session.id, kind)
            return
        handler = (
            memory_control.handle_action
            if kind == protocol.MEMORY_ACTION
            else memory_control.handle_query
        )
        try:
            await send_json(await handler(payload, settings))
        except Exception:  # noqa: BLE001 — a memory panel is never worth the socket
            logger.exception("[%s] %s failed", session.id, kind)
    else:
        logger.debug("[%s] Ignoring unknown control frame: %r", session.id, payload)


def _chosen_keyword(value) -> str | None:
    """A ``set_model`` keyword, or ``None`` for "use the server's default".

    ``None`` on the wire is the deliberate reset — the same meaning it carries in a
    ``set_voice`` patch — and so is any value that isn't a usable keyword or model id.
    Dropping a bad value silently matches every other control frame; the ``model``
    frame that follows says what actually took effect, so nothing is left guessing.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if "/" in value:
        return value if models.valid_model(value) else None
    return value.lower() if models.valid_keyword(value) else None


async def _guarded_turn(
    audio: bytes | None,
    send_json,
    send_bytes,
    session: Session,
    *,
    text: str | None = None,
) -> None:
    """Run one turn, converting failures into an error frame instead of a crash.

    Exactly one of ``audio`` (spoken) or ``text`` (typed) carries the user's input.
    """
    try:
        await run_turn(
            audio,
            send_json,
            send_bytes,
            session.conversation,
            session.client_tools,
            # The session id is this turn's conversation id, so model spend and tool
            # calls are attributable to a session and a peer call joins the same
            # exchange.
            conversation_id=session.id,
            text=text,
            # Read here rather than at frame-parse time so a ``set_voice`` that lands
            # while this turn is speaking applies to the next one, not to sentence 4.
            voice=session.voice,
            # Same discipline for the brain: switching model mid-reply would answer
            # the second half of a question with a different model than the first.
            model=session.model_keyword,
        )
    except asyncio.CancelledError:
        raise  # interrupt/barge-in — expected, let it unwind
    except Exception as exc:  # noqa: BLE001 — surface any turn failure to the client
        logger.exception("[%s] Turn failed", session.id)
        try:
            await send_json(protocol.error(str(exc), protocol.ERR_INTERNAL))
        except Exception:  # noqa: BLE001 — socket may already be gone
            pass
