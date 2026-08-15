"""What Amber sounds like — the TTS knobs, as one value.

`app/tts.py` used to read four fields straight off `settings` at synthesis time,
which made the voice a process-wide constant: changing it meant editing `.env` on
the box and restarting. This module makes it a **value** instead, so it can have a
default from config *and* a per-connection override a client sets over the wire
(``set_voice``, see `app.protocol`). The default is unchanged, so an install that
never sends the frame behaves exactly as before.

Three things live here:

* `VoiceSettings` — model, voice, container, speed, instructions. Frozen, so a
  session's voice is replaced rather than mutated and an in-flight turn can never
  see it change halfway through a reply.
* The **catalogue** (`VOICES`, `MODELS`, `FORMATS`, the speed range). Amber knows
  what the speech endpoint accepts; a client shouldn't have to hardcode it, so it
  goes out on the wire in the ``voice`` frame and a picker can be built from it.
* The **model quirks**, in one place: OpenAI's two TTS generations take opposite
  parameters. ``tts-1``/``tts-1-hd`` accept ``speed`` and reject ``instructions``;
  ``gpt-4o-mini-tts`` is the other way round — it ignores ``speed`` entirely and is
  steered in prose instead. Rather than let "speed" silently mean nothing on the
  newer model, `speech_params` **translates**: a non-default speed becomes a pacing
  sentence prepended to the instructions. One knob, one meaning, both models.

Client-supplied patches are validated against the catalogue and clamped
(`patched`); values from `app/config.py` are not, because that is the operator's
own file and pinning a model this build has never heard of is a legitimate thing
to do there.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.config import Settings, get_settings

# The voices the speech endpoint accepts. The first six pair with every model; the
# rest were introduced alongside ``gpt-4o-mini-tts`` and are most expressive there.
VOICES: tuple[str, ...] = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)

# Newest first — a picker built from this list should lead with the good one.
MODELS: tuple[str, ...] = ("gpt-4o-mini-tts", "tts-1-hd", "tts-1")

# Containers. ``mp3`` is the default because every browser and every Electron
# renderer decodes it without help; the rest are here for clients that want them.
FORMATS: tuple[str, ...] = ("mp3", "opus", "aac", "flac", "wav", "pcm")

# The endpoint's own bounds on ``speed``. Outside these it 400s, so a client patch
# is clamped rather than passed through and turned into a failed turn.
MIN_SPEED = 0.25
MAX_SPEED = 4.0

# Bound on a client-supplied instruction string. It rides on every synthesis call
# of the session, so it is prompt-shaped cost, not a one-off.
MAX_INSTRUCTIONS = 600

# The generational split described in the module docstring.
_NO_INSTRUCTIONS = frozenset({"tts-1", "tts-1-hd"})
_NO_SPEED = frozenset({"gpt-4o-mini-tts"})


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    """One voice configuration: everything the speech call needs but the text."""

    model: str = "tts-1"
    voice: str = "alloy"
    audio_format: str = "mp3"
    speed: float = 1.0
    instructions: str = ""

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "VoiceSettings":
        """The install's default voice, from `app/config.py`."""
        s = settings or get_settings()
        return cls(
            model=s.tts_model,
            voice=s.tts_voice,
            audio_format=s.tts_format,
            speed=_clamp_speed(s.tts_speed),
            instructions=s.tts_instructions.strip()[:MAX_INSTRUCTIONS],
        )

    def patched(self, payload: Any) -> "VoiceSettings":
        """Return a copy with a client's ``set_voice`` patch applied.

        A *patch*: absent keys keep their current value, so a client that only wants
        to change the speed sends only the speed. Every field is validated against
        the catalogue and anything unrecognised is **dropped silently** rather than
        rejected — the same posture as every other malformed control frame, and the
        ``voice`` ack that follows reports what actually took effect, so a client
        never has to guess whether its value survived.

        An explicit ``null`` is the one exception, and it means **reset this field to
        the install default**. Without it "use whatever the server is configured for"
        would be a state a client could leave but never return to, short of dropping
        the socket — a UI with a "server default" option would have to lie about when
        it takes effect.
        """
        if not isinstance(payload, dict):
            return self

        default = VoiceSettings.from_settings()
        patch: dict[str, Any] = {}

        if "voice" in payload:
            voice = payload["voice"]
            if voice is None:
                patch["voice"] = default.voice
            elif isinstance(voice, str) and voice.strip().lower() in VOICES:
                patch["voice"] = voice.strip().lower()

        if "model" in payload:
            model = payload["model"]
            if model is None:
                patch["model"] = default.model
            elif isinstance(model, str) and model.strip() in MODELS:
                patch["model"] = model.strip()

        if "format" in payload:
            audio_format = payload["format"]
            if audio_format is None:
                patch["audio_format"] = default.audio_format
            elif (
                isinstance(audio_format, str)
                and audio_format.strip().lower() in FORMATS
            ):
                patch["audio_format"] = audio_format.strip().lower()

        if "speed" in payload:
            speed = payload["speed"]
            if speed is None:
                patch["speed"] = default.speed
            # bool is an int; a JSON ``true`` here means the client is confused, not
            # that it asked for 1.0x.
            elif isinstance(speed, (int, float)) and not isinstance(speed, bool):
                patch["speed"] = _clamp_speed(float(speed))

        if "instructions" in payload:
            instructions = payload["instructions"]
            if instructions is None:
                patch["instructions"] = default.instructions
            elif isinstance(instructions, str):
                patch["instructions"] = instructions.strip()[:MAX_INSTRUCTIONS]

        return replace(self, **patch) if patch else self

    def as_dict(self) -> dict[str, Any]:
        """The wire shape — note ``format``, matching the ``audio_chunk`` frame."""
        return {
            "model": self.model,
            "voice": self.voice,
            "format": self.audio_format,
            "speed": self.speed,
            "instructions": self.instructions,
        }

    def speech_params(self) -> dict[str, Any]:
        """Keyword arguments for the speech endpoint, minus the text itself.

        Where the model quirks are resolved. ``speed`` is only sent to a model that
        acts on it; on one that doesn't, a non-default speed is folded into the
        instructions as a pacing sentence so the knob still does what it says.
        ``instructions`` is only sent to a model that accepts it at all.
        """
        params: dict[str, Any] = {
            "model": self.model,
            "voice": self.voice,
            "response_format": self.audio_format,
        }

        if self.model not in _NO_SPEED:
            params["speed"] = self.speed

        if self.model not in _NO_INSTRUCTIONS:
            instructions = self.instructions
            if self.model in _NO_SPEED and self.speed != 1.0:
                pace = (
                    f"Speak at about {self.speed:g} times a natural conversational "
                    "pace."
                )
                instructions = f"{pace} {instructions}".strip()
            if instructions:
                params["instructions"] = instructions

        return params

    @property
    def speed_is_native(self) -> bool:
        """True when ``speed`` reaches the endpoint rather than the instructions.

        Surfaced on the wire so a client can say *how* the setting is being applied
        instead of implying every model treats it the same way.
        """
        return self.model not in _NO_SPEED


def _clamp_speed(value: float) -> float:
    """Hold speed inside the endpoint's range, rounding off float noise."""
    return round(min(max(float(value), MIN_SPEED), MAX_SPEED), 2)


def options() -> dict[str, Any]:
    """The catalogue, for the ``voice`` frame.

    Sent so a client can build a picker from what this Amber actually accepts
    rather than shipping its own copy of the list and drifting from it.
    """
    return {
        "voices": list(VOICES),
        "models": list(MODELS),
        "formats": list(FORMATS),
        "speed_range": [MIN_SPEED, MAX_SPEED],
        # Which models take ``speed`` natively. A client showing a speed control can
        # explain the difference instead of quietly meaning two things.
        "native_speed_models": [m for m in MODELS if m not in _NO_SPEED],
        "instruction_models": [m for m in MODELS if m not in _NO_INSTRUCTIONS],
    }
