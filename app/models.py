"""Which brain answers — named by keyword, re-pointable while Amber is running.

Amber has always picked a model by *describing* it rather than naming it:
``llm_tier="balanced"``, resolved by `agent_runtime.model_router` into a concrete
OpenRouter id. That indirection is right and stays. Two things were missing.

**There were three keywords.** ``cheap`` / ``balanced`` / ``strong`` is a capability
*ladder*, and a ladder can only say "better" — it has no way to say "for code" or
"for a long document", which is how you actually choose. This module keeps the
ladder and adds the axes beside it (see `BUILTIN`), and lets a new keyword be
invented at runtime, because the useful ones are personal.

**The table was a Python literal in a library.** Re-pointing ``coding`` at a model
that shipped this morning meant editing `agent_runtime`, bumping the pin, and
redeploying — so in practice it never happened, and the keyword slowly came to mean
whatever was good a year ago. Here the mapping is a *value*: built-in defaults,
overridden per install by rows in SQLite, edited over the wire from Aperture (a
``set_model`` frame, see `app.protocol`) and in effect on the very next turn.

Three layers, in resolution order:

1. a **literal model id** — anything containing ``/`` passes straight through, the
   same escape hatch `model_router` offers, so pinning one call site never requires
   inventing a keyword;
2. this install's **overrides**, from the ``model_keywords`` table;
3. the **built-in defaults** below, and failing those `agent_runtime`'s own table.

Resolution never raises on the turn path. An unknown keyword — a typo in `.env`, a
row for a keyword since deleted — falls back to the install default and logs, because
a mistyped preference must not turn every reply into an error frame.

**About those defaults.** Every keyword ships pointing at one of the three models
this repo already knows are real ids, so several of them start out identical. That
is deliberate rather than lazy: a table of plausible-looking ids invented at authoring
time is worse than useless, since a wrong id fails at request time with nothing to
say why. The keywords are the vocabulary; pointing them at the models *you* rate is
the feature, and Aperture is where you do it.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# The ids this repo has verified — `agent_runtime.model_router`'s own table and its
# fallback price list are built from exactly these three.
_FAST = "anthropic/claude-haiku-4.5"
_CHEAP = "meta-llama/llama-3.1-8b-instruct"
_STRONG = "anthropic/claude-sonnet-4.6"


@dataclass(frozen=True, slots=True)
class Keyword:
    """One way of describing the model you want, and where it currently points."""

    name: str
    description: str
    model: str


# The vocabulary. Order is display order — the ladder first, then the axes, which is
# how a picker should read. Editing this list changes what a *fresh* install starts
# with; it never overrides a keyword an install has already re-pointed.
BUILTIN: tuple[Keyword, ...] = (
    Keyword(
        "fast",
        "First word out quickest. What a spoken turn actually notices.",
        _FAST,
    ),
    Keyword(
        "cheap",
        "The least you can spend and still hold a conversation. Bulk and background work.",
        _CHEAP,
    ),
    Keyword(
        "balanced",
        "The everyday all-rounder, and Amber's own default.",
        _FAST,
    ),
    Keyword(
        "strong",
        "Best general quality, at more latency and more money.",
        _STRONG,
    ),
    Keyword(
        "coding",
        "Writing, reading and fixing code.",
        _STRONG,
    ),
    Keyword(
        "reasoning",
        "Multi-step problems worth thinking slowly about.",
        _STRONG,
    ),
    Keyword(
        "writing",
        "Long-form prose — tone, structure, keeping a voice.",
        _STRONG,
    ),
    Keyword(
        "research",
        "Long tool-using chains: search, read, cross-check, summarise.",
        _STRONG,
    ),
    Keyword(
        "vision",
        "Turns with an image in them.",
        _STRONG,
    ),
    Keyword(
        "long",
        "Very large inputs — a whole document, a whole transcript.",
        _STRONG,
    ),
)

BUILTIN_MODELS: dict[str, str] = {k.name: k.model for k in BUILTIN}
_DESCRIPTIONS: dict[str, str] = {k.name: k.description for k in BUILTIN}

# A keyword is a bare word: lowercase, no slash (which would make it ambiguous with a
# model id) and short enough to be a label in a picker.
_KEYWORD_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# A model id is ``vendor/model``. Requiring the slash is what makes rule 1 of the
# resolution order unambiguous, and it rejects the commonest mistake — pasting a
# keyword into the model field — before it reaches OpenRouter as a 400.
_MODEL_RE = re.compile(r"^[^\s/]+/[^\s]{1,120}$")


def valid_keyword(name: Any) -> bool:
    return isinstance(name, str) and bool(_KEYWORD_RE.match(name.strip().lower()))


def valid_model(model: Any) -> bool:
    return isinstance(model, str) and bool(_MODEL_RE.match(model.strip()))


class _Catalogue:
    """The override table, read from SQLite once and kept in memory.

    Resolution happens on every turn, immediately before the model call, so it must
    not touch the database — hence the cache. Writes go through here too, which is
    what keeps the cache honest: there is no path that changes a mapping without
    also updating what the next turn will read.

    A store that cannot be opened (no database yet, a read-only volume) degrades to
    "no overrides" rather than failing. Losing a customisation is recoverable and
    visible in the ``model`` frame; refusing to answer is neither.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, dict] | None = None
        self._lock = threading.Lock()

    def overrides(self) -> dict[str, dict]:
        """Live overrides as ``{keyword: {model, description, ...}}``.

        Tombstones — rows whose deletion is still queued for the sync store — are
        filtered here, so nothing downstream has to know they exist.
        """
        with self._lock:
            if self._overrides is None:
                self._overrides = self._load()
            return {k: dict(v) for k, v in self._overrides.items() if v.get("model")}

    def _load(self) -> dict[str, dict]:
        try:
            from app.memory import get_store

            rows = get_store().model_keywords()
        except Exception:  # noqa: BLE001 — a missing store is not a failed turn
            logger.warning("Could not read model keyword overrides; using defaults", exc_info=True)
            return {}
        return {
            k: v
            for k, v in rows.items()
            if valid_keyword(k) and (valid_model(v.get("model")) or not v.get("model"))
        }

    def set(self, keyword: str, model: str, *, description: str | None = None) -> None:
        keyword, model = keyword.strip().lower(), model.strip()
        with self._lock:
            if self._overrides is None:
                self._overrides = self._load()
            from app.memory import get_store

            get_store().set_model_keyword(keyword, model, description=description)
            existing = self._overrides.get(keyword, {})
            self._overrides[keyword] = {
                "model": model,
                "description": (
                    description if description is not None else existing.get("description", "")
                ),
                "synced": False,
            }

    def reset(self, keyword: str) -> None:
        """Drop an override. A built-in returns to its default; a custom keyword goes."""
        keyword = keyword.strip().lower()
        with self._lock:
            if self._overrides is None:
                self._overrides = self._load()
            from app.memory import get_store

            get_store().clear_model_keyword(keyword)
            # Re-read rather than popping: the store may have left a tombstone, and
            # the cache has to agree with the row that the sync pass will find.
            self._overrides = self._load()

    def invalidate(self) -> None:
        """Forget the cache. For tests, and for a store swapped underneath us."""
        with self._lock:
            self._overrides = None


_catalogue = _Catalogue()


def invalidate_cache() -> None:
    """Re-read the override table on next use."""
    _catalogue.invalidate()


def keyword_models() -> dict[str, str]:
    """Every keyword this install knows, mapped to the model it currently means."""
    models = dict(BUILTIN_MODELS)
    models.update({k: v["model"] for k, v in _catalogue.overrides().items()})
    return models


def model_sync_status(settings: Settings | None = None) -> dict[str, Any]:
    """How this install stands with the ecosystem's shared table.

    Imported lazily and defensively: `app.model_sync` imports this module, and the
    catalogue has to work — with or without a sync store, in a test, in a script —
    long before anything HTTP is involved.
    """
    try:
        from app.model_sync import status

        return status(settings)
    except Exception:  # noqa: BLE001 — never let a status read break the frame
        return {"enabled": False}


def default_keyword(settings: Settings | None = None) -> str:
    """The keyword a connection uses when it hasn't chosen one (``AMBER_LLM_TIER``)."""
    return (settings or get_settings()).llm_tier


def resolve(keyword: str | None, settings: Settings | None = None) -> str:
    """Return the concrete model id a keyword means right now.

    ``None`` or empty means the install default. A value containing ``/`` is already
    a model id and is returned untouched. An unrecognised keyword falls back — to the
    install default's model, and past that to ``balanced`` — with a warning, never an
    exception: this runs on the turn path, where the cost of guessing wrong is one
    reply from the wrong model and the cost of raising is no reply at all.
    """
    settings = settings or get_settings()
    name = (keyword or "").strip() or settings.llm_tier.strip()

    if "/" in name:
        return name

    models = keyword_models()
    resolved = models.get(name.lower())
    if resolved:
        return resolved

    # Not ours — but `agent_runtime`'s table may still know it, and a tier name
    # configured before this module existed should keep working.
    try:
        from agent_runtime.model_router import resolve as runtime_resolve

        return runtime_resolve(name)
    except Exception:  # noqa: BLE001 — unknown there too; fall through to the default
        pass

    fallback = models.get(settings.llm_tier.strip().lower()) or BUILTIN_MODELS["balanced"]
    logger.warning(
        "Unknown model keyword %r; falling back to %s. Known keywords: %s",
        name,
        fallback,
        ", ".join(sorted(models)),
    )
    return fallback


def set_keyword(keyword: str, model: str, *, description: str | None = None) -> bool:
    """Point a keyword at a model, for this install, persistently.

    Returns ``False`` for a name or id that fails validation — the caller (a client
    over the wire) is told what actually took effect by the ``model`` frame that
    follows, so a dropped value reports itself rather than needing an error path.
    An unknown keyword is *created*: the useful ones are personal, and there is no
    reason a client can name ``coding`` but not ``sql``.
    """
    if not valid_keyword(keyword) or not valid_model(model):
        return False
    _catalogue.set(keyword, model, description=description)
    return True


def reset_keyword(keyword: str) -> bool:
    """Undo an override: a built-in goes back to its default, a custom one disappears."""
    if not valid_keyword(keyword):
        return False
    _catalogue.reset(keyword)
    return True


def apply_map(payload: Any, settings: Settings | None = None) -> list[str]:
    """Apply a ``{keyword: model}`` patch from a client. Returns what changed.

    A ``null`` value means "reset this keyword", mirroring `app.voice`'s patch
    semantics exactly: absent is "leave it", ``null`` is "back to the default". That
    is what makes "Amber's own choice" a state a UI can return to rather than only
    leave. Anything invalid is dropped silently, like every other malformed control
    frame — the ``model`` frame that follows is the truth about what survived.
    """
    if not isinstance(payload, dict):
        return []

    changed: list[str] = []
    for keyword, model in payload.items():
        if not valid_keyword(keyword):
            continue
        try:
            if model is None:
                if reset_keyword(keyword):
                    changed.append(keyword)
            elif set_keyword(keyword, model):
                changed.append(keyword)
        except Exception:  # noqa: BLE001 — a write failure must not drop the socket
            logger.exception("Could not remap keyword %r", keyword)
    return changed


def state(keyword: str | None, settings: Settings | None = None) -> dict[str, Any]:
    """What this connection's brain resolves to — the ``settings`` half of the frame.

    ``chosen`` distinguishes "this client picked ``balanced``" from "this client
    picked nothing and the server is configured for ``balanced``". They look identical
    in the resolved values and are different states: only the first should survive a
    change to ``AMBER_LLM_TIER`` on the box.
    """
    settings = settings or get_settings()
    name = (keyword or "").strip() or settings.llm_tier
    return {
        "keyword": name,
        "model": resolve(name, settings),
        "default_keyword": settings.llm_tier,
        "chosen": bool((keyword or "").strip()),
    }


def options(settings: Settings | None = None) -> dict[str, Any]:
    """The catalogue, for the ``model`` frame.

    Built-ins in declared order, then anything this install invented, alphabetically.
    Each entry carries what it points at *now*, what it points at by default, and
    whether the two differ — enough for a picker to offer "reset" without a second
    round trip, and enough to render a keyword this client's build has never heard of.
    """
    settings = settings or get_settings()
    overrides = _catalogue.overrides()
    names = [k.name for k in BUILTIN]
    names += sorted(n for n in overrides if n not in BUILTIN_MODELS)

    keywords = []
    for name in names:
        default = BUILTIN_MODELS.get(name)
        override = overrides.get(name)
        current = (override or {}).get("model") or default or ""
        keywords.append(
            {
                "name": name,
                "model": current,
                "default_model": default,
                # Amber's own words for a keyword she ships; for one invented
                # elsewhere, whatever description travelled with it through the sync
                # store — which is the only thing that makes a stranger's keyword
                # mean anything here.
                "description": _DESCRIPTIONS.get(name)
                or (override or {}).get("description", ""),
                "custom": name not in BUILTIN_MODELS,
                "overridden": override is not None and current != default,
                # False while a change is still queued for the sync store, so a UI can
                # say "not shared yet" instead of implying every app already agrees.
                "shared": bool((override or {}).get("synced")),
            }
        )

    return {
        "default_keyword": settings.llm_tier,
        "keywords": keywords,
        # How this install stands with the ecosystem's shared table.
        "sync": model_sync_status(settings),
        # Where the other model calls point. They are not per-connection — a memory
        # extraction has no connection — but seeing them is the difference between
        # "Amber uses one model" and knowing three keywords are in play.
        "roles": {
            "brain": settings.llm_tier,
            "memory": settings.memory_tier,
            "maintenance": settings.maintenance_tier,
        },
    }
