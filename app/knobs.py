"""Letting Amber change her own settings when asked — design principle 3.

Everything the Settings page can write, Amber can already *describe* and could not
*do*. "Can you talk slower?" produced a sentence about talking slower. That is the
concrete instance of the ecosystem's third design principle — anything the UI can do,
Amber can do when asked, driving the **same** underlying value rather than a parallel
path — and it has been the repo's stated highest-value gap for a while.

The mechanics turn out to be free. `session.voice` and `session.model_keyword` are read
exactly once per turn, in `main._guarded_turn`, into locals that `run_turn` then holds
frozen for the whole reply. So a tool that mutates the session mid-turn **cannot**
change the voice of sentence four; it lands on the next turn, which is precisely the
behaviour you want and costs nothing to arrange. The same is true of the model: swapping
brains halfway would answer the second half of a question in a different voice than the
first.

What is *not* free is reachability. Amber's registry tools are registered process-wide as
plain functions; `registry.dispatch` passes only the JSON the model produced, `available=`
predicates take no arguments, and there is no contextvar anywhere in this codebase. A
`@registry.register` tool therefore cannot see the connection it is running for, at all.
The two things that solve this already exist: `ClientTools` and `Confirmations` are
per-connection objects threaded explicitly down to `build_broker`, and `_signal_broker`
turns a closure over per-turn state into a `LocalToolBroker`. This module is the object;
`app.brain._knobs_broker` is the broker. `Knobs` is built per turn in `main` and takes the
session **duck-typed** — importing `Session` here would cycle, since `app.session` needs
this module for nothing and `app.main` needs both.

**The confirmation posture is the point, not a detail.** Voice is per-connection and
trivially reversible: if Amber mishears "slower" the next sentence fixes it, and nothing
outside this socket ever knew. Re-pointing a *keyword* is a different animal — it is
install-wide, persisted to SQLite, and pushed to the sync store so `coding` means the same
thing in every app in the ecosystem. One is a knob; the other is a config change made by
remark. So `remap_keyword` carries ``requires_confirmation`` and the voice tools do not,
which makes this the first real use of the approval pair beyond `update_server`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app import models, protocol
from app.config import Settings, get_settings
from app.voice import MAX_SPEED, MIN_SPEED, options as voice_options

logger = logging.getLogger(__name__)


def enabled(settings: Settings | None = None) -> bool:
    return bool((settings or get_settings()).feature_self_settings)


class Knobs:
    """This connection's settings, as something a tool can reach and change.

    Deliberately thin. Every method delegates to the code the *client* frames already
    use — `VoiceSettings.patched`, `models.apply_map` — so a setting changed by asking
    and one changed by clicking are validated by the same lines and land on the same
    value. A second validation path here is exactly the drift this principle exists to
    prevent.
    """

    def __init__(self, session: Any, send_json: Any = None) -> None:
        self._session = session
        self._send = send_json

    async def _echo_voice(self) -> None:
        """Tell the client what the voice is now.

        Not decoration — it is the mechanism that keeps asking and clicking in step.
        `set_voice` from a client is always answered with a `voice` frame, and a
        Settings page reads that frame rather than its own request. A change Amber made
        herself has to answer the same way or the page sits showing a value that is no
        longer true until the next reconnect.

        Never raises: a closed socket costs an echo, not the tool call.
        """
        if self._send is None:
            return
        settings = get_settings()
        with contextlib.suppress(Exception):
            await self._send(
                protocol.voice(
                    self._session.voice.as_dict(),
                    voice_options(),
                    locked=not settings.feature_voice_control,
                )
            )

    async def _echo_model(self) -> None:
        """The same, for which brain is answering."""
        if self._send is None:
            return
        settings = get_settings()
        with contextlib.suppress(Exception):
            await self._send(
                protocol.model(
                    models.state(self._session.model_keyword, settings),
                    models.options(settings),
                    locked=not settings.feature_model_control,
                )
            )

    # --- voice ----------------------------------------------------------------

    async def set_voice(
        self,
        speed: float | None = None,
        voice: str | None = None,
        instructions: str | None = None,
    ) -> str:
        """Patch how Amber sounds on this connection. Never raises.

        Absent arguments are left alone. Values are validated and clamped by
        `VoiceSettings.patched`, which drops anything unrecognised silently — so this
        reports what actually took effect rather than what was asked for, and the model
        can tell the user the truth.
        """
        patch: dict[str, Any] = {}
        if speed is not None:
            patch["speed"] = speed
        if voice is not None:
            patch["voice"] = voice
        if instructions is not None:
            patch["instructions"] = instructions
        if not patch:
            return self.describe()

        before = self._session.voice
        self._session.voice = before.patched(patch)
        after = self._session.voice

        await self._echo_voice()

        if after == before:
            # Everything was dropped as invalid. Say so plainly: silence here would
            # have the model cheerfully confirm a change that did not happen.
            return (
                f"That didn't change anything — speed has to be between {MIN_SPEED} "
                f"and {MAX_SPEED}, and the voice has to be one I have. "
                f"Still {self._describe_voice()}."
            )
        logger.info("Voice changed by request: %s", after.as_dict())
        return f"Done — {self._describe_voice()} from now on."

    # --- which brain ----------------------------------------------------------

    async def set_brain(self, keyword: str | None = None) -> str:
        """Pick the model for *this connection* by keyword. ``None`` resets to default.

        Per-connection and unpersisted, like the voice: the next connection follows the
        install's configured tier again.
        """
        settings = get_settings()
        if keyword is None or not str(keyword).strip():
            self._session.model_keyword = None
            await self._echo_model()
            return f"Back to the default brain ({settings.llm_tier})."

        name = str(keyword).strip().lower()
        if not models.valid_keyword(name):
            return f"'{keyword}' isn't a keyword I recognise. {self._known_keywords()}"
        if name not in models.keyword_models():
            return f"I don't have a '{name}' keyword. {self._known_keywords()}"

        self._session.model_keyword = name
        await self._echo_model()
        resolved = models.resolve(name, settings)
        logger.info("Brain changed by request: %s -> %s", name, resolved)
        return f"Switched to {name} ({resolved}) for this conversation."

    # --- what a keyword *means* (install-wide) --------------------------------

    async def remap_keyword(self, keyword: str, model: str | None = None) -> str:
        """Re-point what a keyword means, for the whole install. Persisted and shared.

        Gated behind confirmation at the broker, because this is not a knob: it is a
        config change that outlives the socket, and `model_sync` pushes it to the store
        so every other app in the ecosystem resolves the keyword the new way.
        """
        name = str(keyword or "").strip().lower()
        if not models.valid_keyword(name):
            return f"'{keyword}' isn't a usable keyword name."
        if model is not None and not models.valid_model(model):
            return (
                f"'{model}' doesn't look like a model id — they're written "
                "'vendor/model', like 'anthropic/claude-sonnet-4.6'."
            )

        settings = get_settings()
        changed = await asyncio.to_thread(
            models.apply_map, {name: model}, settings
        )
        if not changed:
            return f"Couldn't re-point {name}."

        # Already in effect locally; this only tells the rest of the ecosystem.
        from app import model_sync

        model_sync.schedule(settings)
        # The catalogue moved for the whole install, so every connected picker is now
        # showing a stale mapping.
        await self._echo_model()
        if model is None:
            return f"Reset {name} to its built-in default ({models.resolve(name, settings)})."
        logger.info("Keyword re-pointed by request: %s -> %s", name, model)
        return f"{name} now means {model}, everywhere."

    # --- what the prompt says -------------------------------------------------

    def describe(self) -> str:
        """One line naming the settings actually in effect right now.

        Interpolated from live state, following `persona._device_block` — a prompt that
        *described* the settings from memory would drift the moment one changed, and the
        model needs the current speed to know which way "slower" is.
        """
        settings = get_settings()
        keyword = self._session.model_keyword or settings.llm_tier
        return f"{self._describe_voice()}, thinking with the {keyword} model"

    def _describe_voice(self) -> str:
        voice = self._session.voice
        pace = "" if voice.speed == 1.0 else f" at {voice.speed:g}x"
        return f"speaking as {voice.voice}{pace}"

    @staticmethod
    def _known_keywords() -> str:
        return "I know: " + ", ".join(sorted(models.keyword_models()))
