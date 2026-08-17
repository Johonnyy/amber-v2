"""What this Amber can reach, what it is running, and what it has spent.

None of this is new information. `peers.status()` has always known whether discovery
is working and which peers resolved; `model_sync.status()` has always known whether
the keyword table agrees with the ecosystem; the lifespan has always known which
background loops it started. All of it was reachable only from Python, so a client
could infer a peer's existence solely from a tool name it happened to watch go past,
and could not tell "no peers configured" from "the sync store is down" at all.

This gathers it into one advisory frame, following the precedent `voice` and `model`
set: **the server says what is true rather than the client shipping a copy that
drifts.**

Two rules hold this together.

**Never serialise a `PeerRecord` whole.** It carries the peer's bearer token. The
shape of this module — take an object, turn it into a dict — is exactly the shape
that leaks one, so peers are projected field by field and a test asserts no token
survives.

**Every section fails independently.** A status frame is a nicety; an unreachable
sync store or a locked database must cost a panel, never a turn. Each gatherer is
wrapped, and a section that fails is simply absent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import model_sync, models
from app import peers as peer_discovery
from app.config import Settings
from app.session import Session

logger = logging.getLogger(__name__)


async def build(session: Session, settings: Settings) -> dict[str, Any]:
    """Gather everything worth showing. Never raises."""
    sections: dict[str, Any] = {}
    _add(sections, "session", lambda: _session(session, settings))
    _add(sections, "limits", lambda: _limits(session, settings))
    _add(sections, "peers", lambda: _peers(settings))
    _add(sections, "sync", lambda: model_sync.status(settings))
    _add(sections, "features", lambda: _features(settings))
    # The only one that touches the database, so it is the only one worth a thread.
    sections["memory"] = await _memory(settings)
    return sections


def _add(into: dict[str, Any], key: str, gather) -> None:
    try:
        into[key] = gather()
    except Exception:  # noqa: BLE001 — a panel is never worth a turn
        logger.debug("Could not gather status section %r", key, exc_info=True)


def _session(session: Session, settings: Settings) -> dict[str, Any]:
    return {
        "id": session.id,
        "turns": session.turns,
        "max_turns": settings.max_turns_per_session,
        # How much context the brain is carrying, which is the thing that silently
        # grows until a reply gets expensive.
        "history": len(session.conversation.messages),
        "client_tools": list(session.client_tools.names()),
    }


def _limits(session: Session, settings: Settings) -> dict[str, Any]:
    """The turn budget, so a client can show headroom rather than only a refusal.

    Deliberately reports the *policy* and not the limiter's internal window. Reading
    how many events are currently in the window would mean either reaching into
    `RateLimiter`'s private deque or making it purge on a read — and a number that
    decays on its own between two renders is worse than no number, because it looks
    like a bug. `session.turns` against `max_turns` is the honest, monotonic one.
    """
    return {
        "turns": settings.rate_limit_turns,
        "window_s": settings.rate_limit_window_s,
    }


def _peers(settings: Settings) -> dict[str, Any]:
    """Who Amber can call, by name, with no credential anywhere near the wire."""
    state = peer_discovery.status(settings)
    registry = peer_discovery.registry()
    known: list[dict[str, Any]] = []
    for name in peer_discovery.known_peers(settings):
        entry: dict[str, Any] = {"name": name}
        try:
            record = registry.resolve(name)
        except Exception:  # noqa: BLE001 — a peer that won't resolve is still a peer
            record = None
        if record is not None:
            # Field by field, deliberately. `record.as_dict()` would carry `token`,
            # and this is precisely the kind of convenience that leaks one.
            entry["base_url"] = record.base_url
            entry["version"] = record.version
            entry["tools"] = len(record.tools)
        known.append(entry)
    return {
        "enabled": state.get("enabled", False),
        "last_ok": state.get("last_ok"),
        "last_error": state.get("last_error"),
        "discovered": state.get("discovered", 0),
        "known": known,
    }


def _features(settings: Settings) -> dict[str, bool]:
    """Which of the optional halves of Amber are actually running here.

    A panel that shows a Memory section on an install with `AMBER_FEATURE_MEMORY`
    off is lying about the system, and the client cannot know without being told.
    """
    return {
        "tools": settings.feature_tools,
        "memory": settings.feature_memory,
        "memory_control": settings.feature_memory_control,
        "signals": settings.feature_signals,
        "maintenance": settings.feature_maintenance,
        "activity_stream": settings.feature_activity_stream,
        "text_stream": settings.feature_text_stream,
        # Whether Amber can speak first at all, and whether reminders will ever arrive.
        # A client cannot infer either: with push off, a reminder is still accepted and
        # still listed, and the only symptom is that it never turns up.
        "push": settings.feature_push,
        "reminder_delivery": (
            settings.feature_reminder_delivery
            and settings.feature_memory
            and settings.feature_push
        ),
        # Whether to build the approval dialog. With this off, a gated tool simply runs.
        "confirmations": settings.feature_confirmations,
        "mcp_server": settings.mcp_server_enabled,
        "peer_discovery": peer_discovery.discovery_enabled(settings),
        "model_sync": model_sync.sync_enabled(settings),
    }


async def _memory(settings: Settings) -> dict[str, Any]:
    if not settings.feature_memory:
        return {"facts": 0}
    try:
        from app.memory.store import get_store

        return {"facts": await asyncio.to_thread(get_store().fact_count)}
    except Exception:  # noqa: BLE001
        logger.debug("Could not count facts for status", exc_info=True)
        return {}


def roles(settings: Settings) -> dict[str, str]:
    """Which keyword each kind of model call uses. Cheap, and already assembled."""
    return models.options(settings).get("roles", {})
