"""Sharing the keyword table with the rest of the ecosystem.

`app.models` answers "what does ``coding`` mean *here*". This module makes that
answer the same everywhere: the hosted sync store keeps one table of keyword
overrides, every app merges it over its own built-in defaults, and re-pointing a
keyword once moves the whole fleet instead of needing a release per app.

**Local first, always.** The store is an amplifier, never a dependency — the
ecosystem's founding rule is that an app runs standalone with zero knowledge the
ecosystem exists. So every change is written to Amber's own SQLite table first and
takes effect on the next turn whether or not the store is reachable; syncing is a
background pass that reconciles afterwards. With no ``AMBER_MCP_SYNC_STORE_URL``
configured, nothing here ever runs and Amber's table is simply hers.

One pass is push-then-pull:

1. **Push** every row the store has not accepted yet (``synced = 0``). A row with a
   model is a ``PUT``; an empty one is a *tombstone* — a reset made here — and
   becomes a ``DELETE``, after which the row goes for good.
2. **Pull** the whole table and write it down as ``synced = 1``, skipping any row
   still pending locally (a failed push must not be overwritten by the value it was
   trying to replace).
3. **Prune** rows that were in agreement with the store and are no longer in it —
   somebody reset that keyword elsewhere.

Conflicts resolve last-write-wins, which is the honest fit for a single-person
ecosystem: two devices re-pointing ``coding`` within one interval of each other is
not a scenario worth a merge protocol.

Every failure is swallowed and recorded in `status`, which rides out on the ``model``
frame — so an unreachable store shows up in Aperture as "not shared yet" rather than
as a broken turn or a silent divergence.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app import models
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Same five seconds `agent_mcp.sync_client.register()` allows itself. This runs off
# the turn path, but a hung store must not hold a background task open forever.
TIMEOUT_S = 5.0

#: Last outcome, for the ``model`` frame. Deliberately module state rather than a
#: return value: the frame is built in `app.main` from a request that has no idea a
#: sync ever happened.
_status: dict[str, Any] = {
    "enabled": False,
    "last_ok": None,
    "last_error": None,
    "pending": 0,
}


def sync_enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.feature_model_sync and settings.mcp_sync_store_url.strip())


def status(settings: Settings | None = None) -> dict[str, Any]:
    """What a client should show about sharing. Never raises."""
    settings = settings or get_settings()
    enabled = sync_enabled(settings)
    pending = 0
    if enabled:
        try:
            from app.memory import get_store

            pending = len(get_store().model_keywords(pending=True))
        except Exception:  # noqa: BLE001 — status must not be able to fail
            pending = 0
    return {**_status, "enabled": enabled, "pending": pending}


def _endpoint(settings: Settings) -> str:
    return f"{settings.mcp_sync_store_url.strip().rstrip('/')}/models"


def _headers(settings: Settings) -> dict[str, str]:
    token = settings.mcp_sync_store_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def sync_once(settings: Settings | None = None) -> dict[str, Any]:
    """Run one reconciliation. Returns a small report; never raises.

    Safe to call concurrently with a turn — every database touch goes through
    ``asyncio.to_thread`` and the resolver reads a cache that is invalidated at the
    end rather than mutated underneath a request.
    """
    settings = settings or get_settings()
    report = {"pushed": 0, "removed": 0, "pulled": 0, "pruned": 0, "ok": False}
    if not sync_enabled(settings):
        return report

    from app.memory import get_store

    store = get_store()
    url = _endpoint(settings)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=_headers(settings)) as http:
            # 1. Push what this box changed while nobody was listening.
            pending = await asyncio.to_thread(store.model_keywords, pending=True)
            for keyword, row in pending.items():
                if row["model"]:
                    response = await http.put(
                        f"{url}/{keyword}",
                        json={"model": row["model"], "description": row["description"]},
                    )
                    response.raise_for_status()
                    await asyncio.to_thread(store.mark_model_keyword_synced, keyword)
                    report["pushed"] += 1
                else:
                    response = await http.delete(f"{url}/{keyword}")
                    # A 404 means somebody already removed it — the outcome we wanted.
                    if response.status_code not in (200, 204, 404):
                        response.raise_for_status()
                    await asyncio.to_thread(store.drop_model_keyword, keyword)
                    report["removed"] += 1

            # 2. Pull the shared table.
            response = await http.get(url)
            response.raise_for_status()
            remote = (response.json() or {}).get("keywords") or {}
    except Exception as exc:  # noqa: BLE001 — an unreachable store is a normal state
        _status["last_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("Model keyword sync failed: %s", _status["last_error"])
        return report

    local = await asyncio.to_thread(store.model_keywords)

    for keyword, entry in remote.items():
        if not isinstance(entry, dict):
            continue
        model = str(entry.get("model") or "")
        if not models.valid_keyword(keyword) or not models.valid_model(model):
            # One bad row in a shared table must not stop the rest from arriving.
            logger.warning("Ignoring malformed shared keyword %r -> %r", keyword, model)
            continue
        current = local.get(keyword)
        if current is not None and not current["synced"]:
            continue  # a local change is still queued; it wins until it is pushed
        if current is not None and current["model"] == model:
            continue
        await asyncio.to_thread(
            store.set_model_keyword,
            keyword,
            model,
            description=str(entry.get("description") or ""),
            synced=True,
        )
        report["pulled"] += 1

    # 3. Anything we agreed on that has since been removed elsewhere.
    for keyword, row in local.items():
        if row["synced"] and keyword not in remote:
            await asyncio.to_thread(store.drop_model_keyword, keyword)
            report["pruned"] += 1

    models.invalidate_cache()
    report["ok"] = True
    _status["last_ok"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _status["last_error"] = None
    if any(report[k] for k in ("pushed", "removed", "pulled", "pruned")):
        logger.info("Model keyword sync: %s", report)
    return report


_task: asyncio.Task | None = None


def schedule(settings: Settings | None = None) -> None:
    """Sync soon, without making the caller wait.

    Called right after a client re-points a keyword: the local write has already
    taken effect, and this is only about telling everyone else. One task at a time —
    a burst of edits from a settings page should cost one round trip, not one each.
    """
    global _task
    settings = settings or get_settings()
    if not sync_enabled(settings) or (_task is not None and not _task.done()):
        return
    try:
        _task = asyncio.get_running_loop().create_task(sync_once(settings))
    except RuntimeError:
        pass  # no loop (a script, a test) — the periodic pass will pick it up


async def sync_loop(settings: Settings | None = None) -> None:
    """Reconcile on startup and then on a timer. Started by the app's lifespan.

    The first pass is immediate rather than delayed: a keyword re-pointed from
    another device while this box was down should be in effect for the first turn
    after a restart, not five minutes into it.
    """
    settings = settings or get_settings()
    if not sync_enabled(settings):
        return
    while True:
        try:
            await sync_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any one failure
            logger.exception("Model keyword sync pass failed")
        await asyncio.sleep(max(30.0, settings.model_sync_interval_s))
