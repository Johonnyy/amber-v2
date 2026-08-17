"""The maintenance pass — how Amber gets better without anyone intervening.

Memory that only ever grows gets worse: duplicates accumulate, contradictions sit
side by side, and one-off details from months ago crowd out what matters. This runs
periodically and curates it.

Five steps, deterministic ones first so a model failure never costs the cheap wins:

1. **Decay** — forget stale, unused, low-tier facts. Pure SQL. Durable facts are
   never touched; they leave only by being superseded or explicitly forgotten.
2. **Promote** — lift short-tier facts that keep proving useful. Pure SQL. Repeated
   usefulness is the only evidence of durability that doesn't need a model.
3. **Consolidate** — one bounded LLM call over what changed since the last pass,
   merging duplicates and superseding contradictions.
4. **Prune** — age out the raw exchange log and old telemetry, both of which grow
   without bound otherwise.
5. **Self-review** — one LLM call over the turn telemetry, producing a few short
   notes about how conversations are actually going.

Every step is wrapped individually: one failure is recorded and the rest still run.
Every step is idempotent, so running twice back to back is safe and the second run
does nothing. Everything is bounded — facts per pass, changes per pass, reflections
per pass, tokens per call — so cost is predictable rather than proportional to how
long Amber has been running.

**What it deliberately does not do:** rewrite the persona. Amber notices patterns and
writes them down; the prompt in git stays under human control. Whether those notes
are injected into the prompt at all is `AMBER_FEATURE_SELF_NOTES`, off by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import protocol, push
from app.config import Settings, get_settings
from app.memory.store import (
    SOURCE_CONSOLIDATED,
    SOURCE_EXPLICIT,
    MemoryStore,
    get_store,
)
from app.memory.writer import _FENCE_RE

logger = logging.getLogger(__name__)

# Guards against a single pass running twice concurrently (the periodic loop and a
# manual run overlapping). Cross-process safety comes from every step being
# idempotent plus SQLite's own locking — a one-user system doesn't need more.
_lock = asyncio.Lock()


@dataclass
class MaintenanceReport:
    """What one pass actually did. Logged, and returned for the CLI to print."""

    decayed: int = 0
    promoted: int = 0
    merged: int = 0
    superseded: int = 0
    pruned_messages: int = 0
    pruned_events: int = 0
    reflections: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"decayed={self.decayed} promoted={self.promoted} merged={self.merged} "
            f"superseded={self.superseded} pruned_messages={self.pruned_messages} "
            f"pruned_events={self.pruned_events} reflections={self.reflections} "
            f"errors={len(self.errors)}"
        )


_CONSOLIDATE_SYSTEM = """\
You are tidying the long-term memory of a personal assistant named Amber.

You are given numbered facts she currently believes about her user. Find only the
problems that are unambiguous from the text itself:

- DUPLICATES: two or more facts saying the same thing in different words. Keep the
  clearest wording, drop the rest.
- CONTRADICTIONS: a newer fact that plainly replaces an older one (someone moved,
  changed jobs, a pet died). Supersede the old one with the true version.

Do NOT merge facts that are merely related — "Has a dog named Mango" and "Walks the
dog before work" are two facts, not one. Do NOT invent, infer, or reword anything you
aren't merging. When in doubt, leave it alone: a wrong merge silently destroys
something she knows, and the cost of leaving a duplicate is one extra line.

Respond with ONLY a JSON object, and use [] for anything you have nothing for:
{"merge": [{"keep": 12, "drop": [31, 44], "content": "the clearest wording"}],
 "supersede": [{"old": 7, "content": "the corrected fact"}]}
"""

_REVIEW_SYSTEM = """\
You are a personal assistant named Amber, reviewing how your recent conversations
actually went. You are given a summary of your own tool usage and turn telemetry.

Write at most {limit} short observations — one sentence each — about patterns worth
knowing. Good observations are concrete and actionable: a tool that keeps failing, a
kind of request that keeps getting corrected, replies that keep getting interrupted.

Do not write generic advice ("be helpful", "respond faster"). Do not invent anything
the numbers don't show. If the numbers show nothing worth noting, return [].

Respond with ONLY a JSON array of strings.
"""


def _parse_json(raw: str) -> dict | list | None:
    """Parse a model response that may be fenced or padded. ``None`` if unusable."""
    text = _FENCE_RE.sub("", (raw or "").strip()).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Maintenance: unparseable model response")
        return None


async def _run_model(system: str, payload: str, settings: Settings, runner=None) -> str:
    """One tool-less model call. Streamed and joined, like the memory writer."""
    if runner is None:
        from agent_runtime import AgentRunner

        from app.brain import runtime_settings
        from app.models import resolve as resolve_model

        runner = AgentRunner(
            # A keyword (`app.models`), resolved here like the brain's and the
            # writer's — the runtime only knows its own three tiers.
            model=resolve_model(settings.maintenance_tier, settings),
            # No tools, deliberately: a step that tidies memory must not be able to
            # start doing things with it.
            broker=None,
            max_tokens=settings.maintenance_max_tokens,
            settings=runtime_settings(settings),
        )
    return "".join([chunk async for chunk in runner.stream(payload, system=system)])


async def _consolidate(
    store: MemoryStore, settings: Settings, report: MaintenanceReport, runner=None
) -> None:
    """Merge duplicates and supersede contradictions in what changed recently."""
    since = store.last_reflection_at() or (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).isoformat(timespec="seconds")

    facts = await asyncio.to_thread(
        store.facts_changed_since, since, settings.maintenance_max_facts
    )
    if len(facts) < 2:
        return  # nothing can be a duplicate of nothing

    listing = "\n".join(f"{f['id']}. {f['content']}" for f in facts)
    raw = await _run_model(_CONSOLIDATE_SYSTEM, listing, settings, runner)
    plan = _parse_json(raw)
    if not isinstance(plan, dict):
        return

    # Only ids that were actually in the batch may be touched. A model that invents
    # an id must not be able to reach a fact it was never shown.
    allowed = {f["id"]: f for f in facts}
    changes = 0

    for merge in plan.get("merge") or []:
        if changes >= settings.maintenance_max_changes:
            break
        if not isinstance(merge, dict):
            continue
        keep = merge.get("keep")
        if keep not in allowed:
            continue
        content = str(merge.get("content") or allowed[keep]["content"]).strip()
        if content and content != allowed[keep]["content"]:
            await asyncio.to_thread(store.update_fact, keep, content=content)
        for drop in merge.get("drop") or []:
            if changes >= settings.maintenance_max_changes:
                break
            # Never let a merge delete a fact the user stated outright in favour of
            # one the extractor guessed at.
            if drop not in allowed or drop == keep:
                continue
            if allowed[drop]["source"] == SOURCE_EXPLICIT and allowed[keep][
                "source"
            ] != SOURCE_EXPLICIT:
                continue
            if await asyncio.to_thread(store.forget_fact, drop):
                report.merged += 1
                changes += 1

    for item in plan.get("supersede") or []:
        if changes >= settings.maintenance_max_changes:
            break
        if not isinstance(item, dict):
            continue
        old = item.get("old")
        content = str(item.get("content") or "").strip()
        if old not in allowed or not content:
            continue
        new_id = await asyncio.to_thread(
            store.supersede_fact,
            old,
            content,
            tier=allowed[old]["tier"],
            confidence=allowed[old]["confidence"],
            source=SOURCE_CONSOLIDATED,
        )
        if new_id is not None:
            report.superseded += 1
            changes += 1

    if changes >= settings.maintenance_max_changes:
        logger.warning(
            "Maintenance: hit the %d-change cap; the rest waits for the next pass",
            settings.maintenance_max_changes,
        )


async def _self_review(
    store: MemoryStore, settings: Settings, report: MaintenanceReport, runner=None
) -> None:
    """Turn raw telemetry into a few short notes about how things are going."""
    since = store.last_reflection_at() or (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat(timespec="seconds")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows = await asyncio.to_thread(store.event_summary, since)
    if not rows:
        return

    lines = [
        f"{r['kind']} {r['name'] or ''} "
        f"{'ok' if r['ok'] else 'failed' if r['ok'] == 0 else ''}: "
        f"{r['count']} time(s)"
        + (f", avg {r['avg_latency_ms']}ms" if r["avg_latency_ms"] else "")
        for r in rows
    ]
    payload = "Telemetry since the last review:\n" + "\n".join(lines)

    raw = await _run_model(
        _REVIEW_SYSTEM.format(limit=settings.maintenance_max_reflections),
        payload,
        settings,
        runner,
    )
    notes = _parse_json(raw)
    if not isinstance(notes, list):
        return

    for note in notes[: settings.maintenance_max_reflections]:
        if isinstance(note, str) and note.strip():
            written = await asyncio.to_thread(
                store.add_reflection, note.strip(), period_start=since, period_end=now
            )
            if written is not None:
                report.reflections += 1
                # Surface it. These notes have always been written and never seen —
                # `amber://memory/reflections` is reachable only by an agent holding an
                # MCP key, so the one audience who might act on "replies are running
                # long" was the one audience that could not read them.
                #
                # Surfacing, not injecting: `AMBER_FEATURE_SELF_NOTES` still governs
                # whether a reflection reaches the *prompt*, and it stays off by
                # default. That flag is the line between Amber noticing a pattern and
                # Amber editing her own instructions, and showing someone a note does
                # not cross it.
                if push.enabled(settings):
                    await push.enqueue(
                        protocol.PUSH_REFLECTION,
                        note.strip(),
                        ref={"reflection_id": written},
                    )


async def run_maintenance(
    *,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
    runner=None,
) -> MaintenanceReport:
    """Run one full pass. Never raises: a failed step is recorded, not propagated."""
    settings = settings or get_settings()
    report = MaintenanceReport()
    if not settings.feature_memory:
        return report
    store = store or get_store()

    async with _lock:
        # Ordered cheapest and most certain first, so an LLM outage still leaves the
        # store tidier than it found it.
        steps = (
            ("decay", _step_decay),
            ("promote", _step_promote),
            ("consolidate", lambda s, c, r: _consolidate(s, c, r, runner)),
            ("prune", _step_prune),
            ("self_review", lambda s, c, r: _self_review(s, c, r, runner)),
        )
        for name, step in steps:
            try:
                await step(store, settings, report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad step, not a bad pass
                logger.exception("Maintenance step %s failed", name)
                report.errors.append(f"{name}: {type(exc).__name__}: {exc}")

    logger.info("Maintenance pass complete: %s", report.summary())
    return report


async def _step_decay(
    store: MemoryStore, settings: Settings, report: MaintenanceReport
) -> None:
    report.decayed = await asyncio.to_thread(
        store.decay_facts,
        short_ttl_days=settings.fact_short_ttl_days,
        session_ttl_hours=settings.fact_session_ttl_hours,
    )


async def _step_promote(
    store: MemoryStore, settings: Settings, report: MaintenanceReport
) -> None:
    report.promoted = await asyncio.to_thread(
        store.promote_facts, settings.fact_promote_uses
    )


async def _step_prune(
    store: MemoryStore, settings: Settings, report: MaintenanceReport
) -> None:
    report.pruned_messages = await asyncio.to_thread(
        store.prune_conversations, settings.conversation_keep_days
    )
    report.pruned_events = await asyncio.to_thread(
        store.prune_turn_events, settings.signals_keep_days
    )


async def maintenance_loop(settings: Settings | None = None) -> None:
    """Run a pass every ``maintenance_interval_s``, forever.

    Started and cancelled by the FastAPI lifespan. The startup delay matters: the
    test suite spins the app up repeatedly, and without it every one of those would
    immediately run a pass against the real database.
    """
    settings = settings or get_settings()
    logger.info(
        "Maintenance loop started (first pass in %.0fs, then every %.0fs)",
        settings.maintenance_startup_delay_s,
        settings.maintenance_interval_s,
    )
    await asyncio.sleep(settings.maintenance_startup_delay_s)
    while True:
        try:
            await run_maintenance(settings=settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any single failure
            logger.exception("Maintenance pass failed (non-fatal)")
        await asyncio.sleep(settings.maintenance_interval_s)


def main() -> None:
    """Run one pass from the command line: ``python -m app.maintenance``.

    Builds the store directly rather than through ``get_store()``, so a manual run
    can't pick up a cached singleton bound to a different database path. Useful for
    a cron/systemd timer, and for seeing what a pass actually does before trusting
    it to run unattended.
    """
    import logging as _logging

    settings = get_settings()
    _logging.basicConfig(level=settings.log_level.upper())
    store = MemoryStore(settings.memory_db_path)
    try:
        report = asyncio.run(run_maintenance(store=store, settings=settings))
    finally:
        store.close()

    print(report.summary())
    for error in report.errors:
        print(f"  error: {error}")


if __name__ == "__main__":
    main()
