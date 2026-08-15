"""Voice settings: the value, the client patch, and the wire round trip.

Three concerns, and the middle one is where the bugs would live: a ``set_voice``
patch is the only place an untrusted client reaches the synthesis call, so the
tests lean on what it *refuses* as much as what it accepts.
"""

import pytest
from fastapi.testclient import TestClient

import app.config as config_module
import app.pipeline as pipeline
import app.session as session_module
from app import protocol
from app.main import app
from app.memory import MemoryView
from app.voice import MAX_SPEED, MIN_SPEED, VoiceSettings, options


# --- the value ---------------------------------------------------------------

def test_defaults_come_from_config(monkeypatch):
    monkeypatch.setenv("AMBER_TTS_VOICE", "nova")
    monkeypatch.setenv("AMBER_TTS_SPEED", "1.25")
    config_module.get_settings.cache_clear()
    try:
        voice = VoiceSettings.from_settings()
        assert voice.voice == "nova"
        assert voice.speed == 1.25
    finally:
        config_module.get_settings.cache_clear()


def test_config_speed_is_clamped_too():
    """An operator typo in .env shouldn't 400 every single turn."""
    settings = config_module.Settings(tts_speed=99.0, _env_file=None)
    assert VoiceSettings.from_settings(settings).speed == MAX_SPEED


# --- the client patch --------------------------------------------------------

def test_patch_changes_only_what_it_names():
    base = VoiceSettings(model="tts-1", voice="alloy", speed=1.0)
    patched = base.patched({"speed": 1.4})
    assert patched.speed == 1.4
    assert patched.voice == "alloy"  # untouched
    assert base.speed == 1.0  # frozen — the original is not mutated


def test_patch_normalizes_and_clamps():
    voice = VoiceSettings().patched(
        {"voice": "  NOVA ", "speed": 12, "format": "WAV"}
    )
    assert voice.voice == "nova"
    assert voice.speed == MAX_SPEED
    assert voice.audio_format == "wav"
    assert VoiceSettings().patched({"speed": 0.01}).speed == MIN_SPEED


@pytest.mark.parametrize(
    "payload",
    [
        {"voice": "morgan-freeman"},  # not a real voice
        {"model": "whisper-1"},  # a real model, but not a TTS one
        {"format": "wma"},
        {"speed": "fast"},  # a string, not a number
        {"speed": True},  # bool is an int; treat it as confusion, not 1.0
        "not a dict",
        None,
    ],
)
def test_bad_patches_are_ignored_silently(payload):
    base = VoiceSettings()
    assert base.patched(payload) == base


def test_null_resets_a_field_to_the_install_default(monkeypatch):
    """"Use the server's setting" has to be a state you can get back to."""
    monkeypatch.setenv("AMBER_TTS_VOICE", "sage")
    config_module.get_settings.cache_clear()
    try:
        chosen = VoiceSettings.from_settings().patched({"voice": "nova", "speed": 1.6})
        assert chosen.voice == "nova"

        back = chosen.patched({"voice": None, "speed": None})
        assert back.voice == "sage"  # the install default, not the dataclass default
        assert back.speed == VoiceSettings.from_settings().speed
    finally:
        config_module.get_settings.cache_clear()


def test_instructions_are_bounded():
    voice = VoiceSettings().patched({"instructions": "x" * 5000})
    assert len(voice.instructions) == 600


# --- the model quirks --------------------------------------------------------

def test_old_models_get_speed_and_no_instructions():
    params = VoiceSettings(
        model="tts-1", speed=1.3, instructions="be warm"
    ).speech_params()
    assert params["speed"] == 1.3
    assert "instructions" not in params  # tts-1 rejects it


def test_new_model_gets_instructions_and_speed_becomes_pacing():
    """gpt-4o-mini-tts ignores `speed`, so the knob is translated rather than lost."""
    params = VoiceSettings(
        model="gpt-4o-mini-tts", speed=1.3, instructions="Be warm."
    ).speech_params()
    assert "speed" not in params
    assert "1.3 times" in params["instructions"]
    assert "Be warm." in params["instructions"]


def test_new_model_at_default_speed_adds_no_pacing_note():
    params = VoiceSettings(model="gpt-4o-mini-tts", speed=1.0).speech_params()
    assert "instructions" not in params


def test_speed_is_native_reports_which_way_it_went():
    assert VoiceSettings(model="tts-1").speed_is_native
    assert not VoiceSettings(model="gpt-4o-mini-tts").speed_is_native


def test_catalogue_is_self_consistent():
    catalogue = options()
    assert "alloy" in catalogue["voices"]
    assert catalogue["speed_range"] == [MIN_SPEED, MAX_SPEED]
    # Every model takes exactly one of the two pacing mechanisms.
    for model in catalogue["models"]:
        assert (model in catalogue["native_speed_models"]) != (
            model in catalogue["instruction_models"]
        )


# --- the wire ----------------------------------------------------------------

@pytest.fixture
def faked_io(monkeypatch):
    """The voice loop with STT/LLM/TTS faked — synthesis records what it was given."""
    used: list[VoiceSettings] = []

    async def fake_transcribe(audio, **kw):
        return "hello amber"

    async def fake_synthesize(text, voice=None):
        used.append(voice)
        return b"AUDIO"

    async def fake_think(messages, system=None, **kwargs):
        yield "Hello back."

    async def no_context(query=None, **kw):
        return MemoryView()

    async def no_remember(user_text, reply, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    monkeypatch.setattr(pipeline, "think", fake_think)
    monkeypatch.setattr(pipeline, "build_memory_view", no_context)
    monkeypatch.setattr(pipeline, "remember", no_remember)
    return used


@pytest.fixture
def fresh_caches():
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()
    yield
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()


def _handshake(ws) -> dict:
    assert ws.receive_json()["type"] == protocol.READY
    frame = ws.receive_json()
    assert frame["type"] == protocol.VOICE
    return frame


def test_voice_frame_follows_ready_with_the_catalogue(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _handshake(ws)
        assert frame["settings"]["voice"] == "alloy"
        assert "nova" in frame["options"]["voices"]
        assert "locked" not in frame  # control is on by default


def test_set_voice_is_acknowledged_and_reaches_synthesis(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_voice", "voice": "nova", "speed": 1.5})

        ack = ws.receive_json()
        assert ack["type"] == protocol.VOICE
        assert ack["settings"]["voice"] == "nova"
        assert ack["settings"]["speed"] == 1.5

        ws.send_bytes(b"audio")
        for _ in range(20):
            frame = ws.receive_json()
            if frame["type"] == protocol.AUDIO_CHUNK:
                ws.receive_bytes()
                break
        assert faked_io[0].voice == "nova"
        assert faked_io[0].speed == 1.5


def test_audio_chunk_reports_the_requested_container(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_voice", "format": "wav"})
        ws.receive_json()  # ack

        ws.send_bytes(b"audio")
        for _ in range(20):
            frame = ws.receive_json()
            if frame["type"] == protocol.AUDIO_CHUNK:
                assert frame["format"] == "wav"
                ws.receive_bytes()
                return
        raise AssertionError("never saw an audio_chunk")


def test_out_of_range_speed_is_clamped_on_the_wire(faked_io):
    with TestClient(app).websocket_connect("/ws") as ws:
        _handshake(ws)
        ws.send_json({"type": "set_voice", "speed": 99})
        assert ws.receive_json()["settings"]["speed"] == MAX_SPEED


def test_voice_survives_a_reconnect_with_the_same_session(faked_io):
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        session_id = ws.receive_json()["session_id"]
        assert ws.receive_json()["type"] == protocol.VOICE
        ws.send_json({"type": "set_voice", "voice": "sage"})
        ws.receive_json()

    with client.websocket_connect(f"/ws?session_id={session_id}") as ws:
        ready = ws.receive_json()
        assert ready["resumed"] is True
        # The resumed session speaks the way it did before the drop, and says so.
        assert ws.receive_json()["settings"]["voice"] == "sage"


def test_disabled_flag_pins_the_voice_and_says_so(faked_io, fresh_caches, monkeypatch):
    monkeypatch.setenv("AMBER_FEATURE_VOICE_CONTROL", "false")
    config_module.get_settings.cache_clear()
    session_module.get_session_manager.cache_clear()

    with TestClient(app).websocket_connect("/ws") as ws:
        frame = _handshake(ws)
        assert frame["locked"] is True
        ws.send_json({"type": "set_voice", "voice": "nova"})
        ack = ws.receive_json()
        assert ack["settings"]["voice"] == "alloy"  # unchanged
        assert ack["locked"] is True
