"""Tests for the config system."""

import app.config as config_module
from app.config import Settings


def _fresh(**env) -> Settings:
    """Build Settings from an explicit env mapping, ignoring any real .env file."""
    return Settings(_env_file=None, **env)


def test_defaults_are_sane():
    s = _fresh()
    assert s.stt_model == "whisper-1"
    assert s.tts_model == "tts-1"
    assert s.tts_format == "mp3"
    assert s.feature_stt is True
    assert s.auth_enabled is False


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("AMBER_TTS_VOICE", "nova")
    monkeypatch.setenv("AMBER_FEATURE_STT", "false")
    monkeypatch.setenv("AMBER_PORT", "9001")
    s = Settings(_env_file=None)
    assert s.tts_voice == "nova"
    assert s.feature_stt is False
    assert s.port == 9001


def test_tool_defaults():
    s = _fresh()
    assert s.feature_tools is True
    # Native server-side web search is the default (reliable for current events).
    assert s.search_provider == "anthropic"
    assert s.search_tool_version == "web_search_20250305"
    assert s.search_max_uses == 5
    assert s.openclaw_url == ""  # bridge disabled until configured
    assert s.max_tool_iterations == 5


def test_turn_based_defaults():
    s = _fresh()
    assert s.feature_turn_based is True
    assert s.recall_messages == 12
    # Cold-start recap was bumped for richer cross-session continuity.
    assert s.recent_recap_messages == 8


def test_auth_enabled_follows_secret():
    assert _fresh(auth_secret="").auth_enabled is False
    assert _fresh(auth_secret="hunter2").auth_enabled is True


def test_get_settings_is_cached():
    config_module.get_settings.cache_clear()
    a = config_module.get_settings()
    b = config_module.get_settings()
    assert a is b
