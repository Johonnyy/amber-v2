"""FastAPI application + the WebSocket voice endpoint.

This is the thin transport layer. All the real work lives in `app.pipeline`; here
we just accept the socket, route frames, and wire interruption.

Every client speaks the protocol in `app.protocol`:
  * binary frame in  = a recorded utterance -> triggers a turn
  * ``{"type": "interrupt"}`` in = stop speaking the current turn
  * binary while already speaking = barge-in (cancel current turn, start the new one)
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status

from app import protocol
from app.config import get_settings
from app.pipeline import run_turn
from app.session import Conversation

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("amber")

app = FastAPI(title="Amber", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for systemd / load balancers."""
    return {"status": "ok", "service": "amber", "version": app.version}


@app.websocket("/ws")
async def voice_socket(websocket: WebSocket) -> None:
    if settings.auth_enabled:
        token = websocket.query_params.get("token", "")
        if token != settings.auth_secret:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            logger.warning("Rejected WS connection: bad/missing token")
            return

    await websocket.accept()
    logger.info("Client connected")
    await websocket.send_json(protocol.ready())

    # send helpers bound to this socket, passed into the pipeline
    async def send_json(payload: dict) -> None:
        await websocket.send_json(payload)

    async def send_bytes(data: bytes) -> None:
        await websocket.send_bytes(data)

    # Conversation history for this connection only (Phase 2). Dies with the socket.
    conversation = Conversation()
    current_turn: asyncio.Task | None = None

    async def cancel_current(reason: str) -> None:
        nonlocal current_turn
        if current_turn and not current_turn.done():
            logger.info("Interrupting current turn (%s)", reason)
            current_turn.cancel()
            try:
                await current_turn
            except asyncio.CancelledError:
                pass
        current_turn = None

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is not None:
                # New utterance. Barge-in: drop any in-flight turn first.
                await cancel_current("barge-in")
                current_turn = asyncio.create_task(
                    _guarded_turn(data, send_json, send_bytes, conversation)
                )
                continue

            text = message.get("text")
            if text is not None:
                await _handle_control(text, cancel_current)

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    finally:
        await cancel_current("connection closing")
        logger.info("Session ended")


async def _handle_control(text: str, cancel_current) -> None:
    """Handle an inbound JSON control frame."""
    import json

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Ignoring non-JSON text frame: %r", text[:120])
        return

    if payload.get("type") == protocol.INTERRUPT:
        await cancel_current("client interrupt")
    else:
        logger.debug("Ignoring unknown control frame: %r", payload)


async def _guarded_turn(audio: bytes, send_json, send_bytes, conversation) -> None:
    """Run one turn, converting failures into an error frame instead of a crash."""
    try:
        await run_turn(audio, send_json, send_bytes, conversation)
    except asyncio.CancelledError:
        raise  # interrupt/barge-in — expected, let it unwind
    except Exception as exc:  # noqa: BLE001 — surface any turn failure to the client
        logger.exception("Turn failed")
        try:
            await send_json(protocol.error(str(exc)))
        except Exception:  # noqa: BLE001 — socket may already be gone
            pass
