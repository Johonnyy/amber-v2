"""Finding peers Amber was never told about.

`AMBER_MCP_PEERS` answers "who may I call" as a fact of this box's configuration.
This module makes the sync store a second answer to the same question, so an app that
registers itself becomes callable without an env file being edited and a container
being restarted.

WHAT WAS ACTUALLY BROKEN. `app.brain.build_broker` built its MCP client like this::

    peers = load_static_peers(settings.mcp_peers, settings.mcp_peer_token)
    if peers:
        brokers.append(MCPClient(list(peers), resolver=peers))

Two independent things wrong with those three lines, and either alone is fatal. The
``if peers:`` means an empty ``AMBER_MCP_PEERS`` builds no client at all — not an
empty one, none. And passing the dict as ``resolver`` makes
`agent_runtime.MCPClient._resolve` take its ``Mapping`` branch, which returns before
it would ever fall through to `agent_mcp.registry.resolve`. So the discovered layer
was unreachable even for a box that *did* have static peers.

The consequence is the worst shape a bug can have: **nothing reports it**. A peer
that is not in the map is not a tool that fails, it is a tool that was never offered.
Bloom registered, appeared in ``GET /servers``, mounted its MCP server and answered
401 to an unauthenticated probe — every check anything performs — while Amber named
her own thirteen tools and no ``bloom__*`` ones, and no log line anywhere on either
side said why.

HOW THIS FIXES IT. All the machinery already existed and none of it was wired:

* the store keeps one credential per server, settable only through
  ``PUT /servers/{name}/token`` and returned by ``GET /servers``;
* `agent_mcp.PeerRegistry` is a two-layer cache — static in front of discovered —
  whose ``resolve`` is synchronous and does no I/O, so it is safe on the turn path;
* `agent_runtime.MCPClient` falls through to that registry whenever it is handed a
  *callable* resolver, or none at all.

So this module owns the write half (a background pull into the discovered layer) and
`app.brain` hands the client ``registry.resolve`` instead of a frozen dict.

**Static still wins.** That is `agent_mcp`'s contract and worth keeping: pointing
Amber at a local peer during an incident must not be silently undone by the next
pull. ``build_broker`` re-asserts the static layer every turn for that reason.

**Discovery is an amplifier, never a dependency** — the same rule `app.model_sync`
follows. With no ``AMBER_MCP_SYNC_STORE_URL`` this module never runs and the static
map is simply the whole answer. A refresh that fails leaves the previous list in
place; `PeerRegistry.refresh` never raises, by its own contract, because an app whose
registry is unreachable has to keep serving with what it already knows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from agent_mcp.registry import PeerRegistry, default_registry, load_static_peers

from app.config import APP_NAME, Settings, get_settings

logger = logging.getLogger(__name__)

#: Last outcome, for logs and tests. Module state rather than a return value for the
#: same reason `app.model_sync` keeps one: the readers are nowhere near the writer.
_status: dict[str, Any] = {
    "enabled": False,
    "last_ok": None,
    "last_error": None,
    "discovered": 0,
}

_task: asyncio.Task | None = None


def registry() -> PeerRegistry:
    """The process-wide peer cache.

    Deliberately `agent_mcp`'s own module-level registry rather than one of Amber's:
    `agent_runtime.MCPClient` falls back to ``agent_mcp.registry.resolve`` — the bare
    function over that same object — when no resolver is given, and any peer lookup
    that happens outside `build_broker` goes through it too. A second registry here
    would be a second answer to "where is bloom", which is exactly the class of
    problem `app.config`'s one-prefix rule exists to prevent.
    """
    return default_registry()


def known_peers(settings: Settings | None = None) -> list[str]:
    """Every peer this install can reach — the static map unioned with the discovered.

    **The** list. Both readers go through here: `app.brain.build_broker`, which turns
    it into tools, and `app.ecosystem`, which turns it into a sentence in the system
    prompt. That is not tidiness, it is the bug they had. Amber's prompt told her
    Bloom "is what specialised work gets handed to" while her tool list had no
    ``bloom__*`` in it, so she answered "I can hand work off to the bloom agent" and
    then, asked to actually do it, invented npm commands for a Python service. A
    model cannot tell the difference between a capability it has and one its prompt
    asserts; the only fix is for the two to be computed once.

    Re-asserting the static layer is the reason this is not a pure read. It is a dict
    copy, no I/O, and it has to happen somewhere every turn so that removing a peer
    from ``AMBER_MCP_PEERS`` does not leave it resolving for the life of the process.
    Doing it here means it happens whether the prompt or the broker is built first.

    **Amber is not her own peer.** She registers with the sync store, so ``GET
    /servers`` hands her own record straight back and she loaded it like any other —
    acquiring ``amber__add_task`` beside the in-process ``add_task``, a duplicate
    reachable only by going out to the network and back to the same process. That is
    the invariant `AnthropicRegistryBroker` exists to hold ("Amber never makes an
    HTTP call to herself to add a task"), and discovery quietly broke it the moment
    it started working. The only symptom was her offering to hand work off to "the
    Amber agent".

    `AgentMCPServer` sets this itself, so every app on the library is covered without
    asking — but only if it builds a server, and Amber's needs ``AMBER_MCP_KEYS`` to
    mount at all. Asserting it here too costs a string compare and covers the install
    that has discovery on and its own server off, where a registration written by an
    earlier run can outlive the process that wrote it.
    """
    settings = settings or get_settings()
    reg = registry()
    reg.set_self_name(APP_NAME)
    reg.set_static(load_static_peers(settings.mcp_peers, settings.mcp_peer_token))
    return reg.known()


def discovery_enabled(settings: Settings | None = None) -> bool:
    """Whether a pull is worth attempting at all."""
    settings = settings or get_settings()
    return bool(
        settings.feature_tools
        and settings.feature_peer_discovery
        and settings.mcp_sync_store_url.strip()
    )


def status(settings: Settings | None = None) -> dict[str, Any]:
    """A copy of the last outcome, with `enabled` resolved fresh."""
    settings = settings or get_settings()
    return {**_status, "enabled": discovery_enabled(settings)}


async def refresh_once(settings: Settings | None = None) -> int:
    """Pull the peer list into the discovered layer. Returns how many were loaded.

    Never raises. `PeerRegistry.refresh` already swallows every network and parse
    failure by contract and returns 0; the try here covers the rest, because this is
    called from a background loop whose job is to outlive any single failure.
    """
    settings = settings or get_settings()
    if not discovery_enabled(settings):
        return 0
    try:
        count = await registry().refresh(
            settings.mcp_sync_store_url,
            token=settings.mcp_sync_store_token,
        )
    except Exception as exc:  # noqa: BLE001 — discovery must never break serving
        _status["last_error"] = str(exc)[:200]
        logger.warning("Peer discovery failed: %s", exc)
        return 0

    # A zero is not automatically an error: an ecosystem with one app in it has no
    # peers, and reporting that as a failure would make a healthy box look broken.
    # `refresh` logs its own warning when the *request* failed, which is the case
    # that matters, so this records the outcome and does not editorialise.
    _status["last_ok"] = datetime.now(timezone.utc).isoformat()
    _status["last_error"] = None
    _status["discovered"] = count
    return count


def schedule(settings: Settings | None = None) -> None:
    """Refresh soon, without making the caller wait.

    For the moment a peer is known to have changed — a client wiring one up, a
    registration that just landed — so the next turn sees it rather than the next
    five-minute pass. One task at a time.
    """
    global _task
    settings = settings or get_settings()
    if not discovery_enabled(settings) or (_task is not None and not _task.done()):
        return
    try:
        _task = asyncio.get_running_loop().create_task(refresh_once(settings))
    except RuntimeError:
        pass  # no loop (a script, a test) — the periodic pass will pick it up


async def refresh_loop(settings: Settings | None = None) -> None:
    """Pull on startup and then on a timer. Started by the app's lifespan.

    The first pass is immediate, and that is the point rather than a nicety: a peer
    connected from Aperture while this box was restarting should be callable on the
    first turn after it comes back, not five minutes into it.
    """
    settings = settings or get_settings()
    if not discovery_enabled(settings):
        return
    while True:
        try:
            await refresh_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any one failure
            logger.exception("Peer discovery pass failed")
        await asyncio.sleep(max(30.0, settings.peer_sync_interval_s))
