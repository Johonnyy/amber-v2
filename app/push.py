"""Letting Amber speak first.

Every frame in `app.protocol` before this one is a reply. A client sends an utterance
and gets a transcript, sends `set_voice` and gets `voice`, sends a tool result and the
turn continues. Nothing in Amber could originate a message, and that single fact was the
shared root of a surprising number of half-built things: a reminder could be recorded,
listed and completed but never *fire*; the maintenance pass wrote self-review notes that
only an agent holding an MCP key could read; a Bloom build could finish while you were
on another tab and nothing said so.

This is the missing direction. Two pieces:

* **The outbox** — a SQLite table, not a queue. Every other advisory frame describes a
  turn in progress, so a client that misses one has missed nothing that outlives the
  turn. A push is the opposite: a reminder due at 17:30 while the socket is down is
  precisely the case that has to survive, and Amber restarts on every deploy, so
  in-memory would lose exactly the deliveries that matter. `app.signals` is the model
  for the *loop shape* here and deliberately not for the storage — its bounded queue
  drops rows when full, which is right for telemetry and wrong for a reminder.

* **The deliverer** — a process-wide registry of live sinks. Background tasks hold no
  socket: `Session` has no websocket reference, and `session.client_tools.bind(send_json)`
  was the only place a live connection was ever reachable from outside its own handler.
  This generalises that bind/unbind pair into something a scheduler can reach.

Three rules hold it together.

**A push never lands mid-turn.** This is a correctness rule, not politeness. The pipeline
emits an ``audio_chunk`` frame and then the binary bytes it describes, and the protocol
promises the *next* binary frame is that sentence — with an ``await`` boundary in
between. A background task writing there would corrupt the audio stream for every client.
A lock around each send does **not** fix it, since the pusher would simply take the lock
in the gap; waiting for the connection to be idle removes the hazard rather than
narrowing it. In-turn writers (``activity``, ``delta``, ``confirm_request``) are safe by
construction because they run on the task already driving the turn.

**Delivery is at-least-once, and ``id`` is stable.** If the socket accepts a frame and
the process dies before the row is marked, the same id arrives again on reconnect. The
client dedupes. The alternative — marking delivered before sending — would silently drop
the reminders this whole module exists to protect.

**A failing sink is dropped immediately.** Once a send raises on a closed socket,
starlette latches the connection DISCONNECTED and *every* later send raises, including
ones on unrelated code paths. So a sink that raises is unbound on the spot rather than
retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app import protocol
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

SendJson = Callable[[dict], Awaitable[None]]
IsIdle = Callable[[], bool]


def new_id() -> str:
    """A push id: short, unguessable, and stable across redeliveries."""
    return f"p_{secrets.token_hex(6)}"


def enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).feature_push)


@dataclass
class _Sink:
    """One live connection Amber can write to."""

    session_id: str
    send: SendJson
    is_idle: IsIdle
    #: Identifies *this connection*, not this session. A session outlives its socket
    #: (that is what `?session_id=` resumes), so a reconnect can bind a new sink while
    #: the old handler is still unwinding — and without this its `finally` would unbind
    #: the live one, leaving a connected client that silently never receives a push.
    token: object = None

    def idle(self) -> bool:
        """Whether this connection is between turns. Never raises."""
        try:
            return bool(self.is_idle())
        except Exception:  # noqa: BLE001 — an unreadable gate means "don't write"
            return False


class Deliverer:
    """Live sinks, plus the pass that drains the outbox into them."""

    def __init__(self) -> None:
        self._sinks: dict[str, _Sink] = {}
        # Set when something is enqueued, so a reminder does not wait out the whole
        # interval before anyone hears about it.
        self._wake: asyncio.Event | None = None

    # --- connection lifecycle (mirrors ClientTools.bind/unbind) ----------------

    def bind(
        self, session_id: str, send: SendJson, is_idle: IsIdle, *, token: object = None
    ) -> None:
        """Attach a live connection. Replaces any sink already held for this session.

        ``token`` identifies the connection so a later `unbind` can tell whether it is
        detaching its own socket or someone else's — see `_Sink.token`.
        """
        self._sinks[session_id] = _Sink(session_id, send, is_idle, token)
        logger.debug("[%s] Push sink bound (%d live)", session_id, len(self._sinks))

    def unbind(self, session_id: str, *, token: object = None) -> None:
        """Detach on disconnect. Idempotent — a raising sink unbinds itself too.

        A no-op when the bound sink belongs to a *different* connection, which is the
        reconnect case: resuming a session mints a new socket while the old handler may
        still be in its `finally`, and letting that one win would disconnect the client
        that is actually here.
        """
        current = self._sinks.get(session_id)
        if current is None:
            return
        if token is not None and current.token is not None and current.token is not token:
            logger.debug("[%s] Stale push unbind ignored", session_id)
            return
        del self._sinks[session_id]
        logger.debug("[%s] Push sink unbound (%d live)", session_id, len(self._sinks))

    def live(self) -> int:
        return len(self._sinks)

    # --- the wake-up nudge ----------------------------------------------------

    def _event(self) -> asyncio.Event:
        # Lazily, like `signals._get_queue`, so it binds to whatever loop is running
        # rather than whichever one happened to be current at import.
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    def nudge(self) -> None:
        """Ask the delivery loop to run now. Synchronous and never raises."""
        with contextlib.suppress(RuntimeError):
            self._event().set()

    # --- delivery -------------------------------------------------------------

    async def flush(self, *, session_id: str | None = None) -> int:
        """Send undelivered pushes to every idle live sink. Returns how many landed.

        ``session_id`` restricts delivery to one connection — that is the reconnect
        case, where a client that has just finished its handshake should receive what
        piled up while it was away without also re-broadcasting to everyone else.

        Never raises: a locked database or a dead socket costs a notification, not the
        connection it would have arrived on.
        """
        settings = get_settings()
        if not enabled(settings):
            return 0

        targets = self._targets(session_id)
        if not targets:
            return 0

        try:
            rows = await asyncio.to_thread(_store().pending_pushes, settings.push_batch)
        except Exception:  # noqa: BLE001
            logger.debug("Could not read the push outbox", exc_info=True)
            return 0
        if not rows:
            return 0

        sent = 0
        for row in rows:
            frame = protocol.push(
                row["id"],
                row["kind"],
                row["text"],
                created_at=row.get("created_at"),
                ref=row.get("ref"),
                title=row.get("title"),
            )
            # Re-read each time: a sink can unbind itself mid-loop by raising.
            delivered = await self._fan_out(frame, self._targets(session_id))
            if not delivered:
                # Nobody took it. Leave it pending — that is the entire point of the
                # outbox — and stop, since the next row would fare no better.
                break
            # Marked *after* the send, so a crash in between re-delivers rather than
            # loses. The client dedupes on the id.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_store().mark_push_delivered, row["id"])
            sent += 1

        if sent:
            logger.info("Delivered %d push(es)", sent)
        return sent

    def _targets(self, session_id: str | None) -> list[_Sink]:
        """Idle sinks eligible for delivery right now."""
        if session_id is not None:
            sink = self._sinks.get(session_id)
            return [sink] if sink is not None and sink.idle() else []
        return [s for s in self._sinks.values() if s.idle()]

    async def _fan_out(self, frame: dict[str, Any], targets: list[_Sink]) -> bool:
        """Write one frame to every target. True if at least one accepted it.

        Amber is single-user, so the honest rule is: a push goes to whoever is
        listening. A row is settled once *any* sink takes it, which means a device that
        connects later will not see it. Per-session delivery tracking is a lot of
        machinery for one person; if a second device ever genuinely matters, that is
        the change to make.
        """
        ok = False
        for sink in targets:
            try:
                await sink.send(frame)
                ok = True
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # The socket is gone and starlette has latched it — every later send
                # would raise too, on paths that have nothing to do with pushes.
                logger.debug(
                    "[%s] Push sink failed; unbinding", sink.session_id, exc_info=True
                )
                # By token, so a send that fails on a socket already replaced by a
                # reconnect doesn't take the live one down with it.
                self.unbind(sink.session_id, token=sink.token)
        return ok

    async def deliver_loop(self, interval_s: float | None = None) -> None:
        """Drain the outbox forever. Started and cancelled by the lifespan."""
        settings = get_settings()
        interval = max(1.0, interval_s or settings.push_interval_s)
        logger.info("Push deliverer started (every %.0fs)", interval)
        expiry_due = 0
        try:
            while True:
                # Wake early when something is enqueued; otherwise poll, which is what
                # catches a client reconnecting with a backlog already waiting.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._event().wait(), timeout=interval)
                self._event().clear()
                try:
                    await self.flush()
                    # Roughly hourly, not every tick — it is pure housekeeping.
                    expiry_due += 1
                    if expiry_due * interval >= 3600:
                        expiry_due = 0
                        await asyncio.to_thread(
                            _store().expire_pushes, settings.push_keep_days
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — the loop outlives any single failure
                    logger.exception("Push delivery pass failed (non-fatal)")
        except asyncio.CancelledError:
            # One last attempt on the way out, so a clean restart doesn't strand
            # something that was ready to go.
            with contextlib.suppress(Exception):
                await self.flush()
            raise


@lru_cache
def get_deliverer() -> Deliverer:
    """The process-wide deliverer, shared by every connection and background task."""
    return Deliverer()


def _store():
    from app.memory.store import get_store

    return get_store()


async def enqueue(
    kind: str,
    text: str,
    *,
    ref: dict[str, Any] | None = None,
    title: str | None = None,
) -> str | None:
    """Durably record one thing to say, and ask the deliverer to try now.

    Returns the push id, or ``None`` if it could not be recorded. Never raises — a
    caller is a maintenance pass or a scheduler, and neither should fail because a
    notification could not be filed.
    """
    text = (text or "").strip()
    if not text:
        return None
    push_id = new_id()
    try:
        await asyncio.to_thread(
            _store().add_push, push_id, kind, text, title=title, ref=ref
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not enqueue a %s push (non-fatal)", kind)
        return None
    get_deliverer().nudge()
    return push_id


async def acknowledge(payload: dict[str, Any]) -> None:
    """Apply one ``push_ack``. Never raises.

    ``complete`` is the interesting action: it resolves the push's ``ref`` and calls the
    *same* store function the ``complete_reminder`` tool calls, so a reminder finished by
    tapping and one finished by asking are the same row in the same state. That is the
    standing rule `app.memory_control` follows for facts, applied here.
    """
    push_id = payload.get("id")
    action = payload.get("action", protocol.ACK_SEEN)
    if not isinstance(push_id, str) or not push_id:
        logger.debug("Ignoring malformed push_ack: %r", payload)
        return

    # An ack settles the row whatever the action — a client that acknowledges something
    # has plainly received it, even if the outbox had not been marked yet.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(_store().mark_push_delivered, push_id)

    if action not in (protocol.ACK_COMPLETE, protocol.ACK_DISMISS):
        return

    try:
        row = await asyncio.to_thread(_store().get_push, push_id)
    except Exception:  # noqa: BLE001
        logger.debug("Could not read push %s for its ack", push_id, exc_info=True)
        return
    row = row or {}
    ref = row.get("ref") or {}

    # A dismissed reflection card retires the note itself. The ack has always carried
    # `ref.reflection_id` and this branch was the missing half — dismissing a card did
    # nothing, which made the maintenance pass's notes un-actionable from the one
    # surface that showed them.
    if row.get("kind") == protocol.PUSH_REFLECTION:
        reflection_id = ref.get("reflection_id")
        # Either verb retires the note — waving a self-review card away *is* the
        # judgement. `bool` is an `int` in Python, so the same guard the reminder
        # branch uses applies here.
        if isinstance(reflection_id, int) and not isinstance(reflection_id, bool):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_store().dismiss_reflection, reflection_id)
        return

    # Everything below acts on a reminder, and only `complete` does.
    if action != protocol.ACK_COMPLETE:
        return

    # **Only a reminder push may complete a reminder.** `POST /push` already refuses
    # to let a caller claim `kind="reminder"`, but `ref` is passed through as given —
    # so without this check an authenticated peer could post an innocuous notice
    # carrying `{"reminder_id": 7}` and have one tap on it silently tick off an
    # unrelated reminder. The kind is the part Amber controls; trust that, not the ref.
    if row.get("kind") != protocol.PUSH_REMINDER:
        return
    reminder_id = ref.get("reminder_id")
    # `bool` is an `int` in Python, and `{"reminder_id": true}` would otherwise
    # complete reminder #1.
    if isinstance(reminder_id, int) and not isinstance(reminder_id, bool):
        with contextlib.suppress(Exception):
            done = await asyncio.to_thread(_store().complete_reminder, reminder_id)
            logger.info(
                "Reminder #%s completed from a push ack: %s",
                reminder_id,
                "ok" if done else "already done",
            )


def reset_for_tests() -> None:
    """Drop the process-wide deliverer so one test's sinks can't leak into another's."""
    get_deliverer.cache_clear()
