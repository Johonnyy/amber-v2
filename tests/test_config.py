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
    # Self-dispatched search: the native Anthropic server tool went with the brain
    # swap, since a server tool only exists inside a provider's own request loop.
    # "auto" resolves to tavily when a key is present and duckduckgo when it isn't,
    # so a default install searches without configuration and a keyed one searches
    # well — see app/tools/search.py::resolve_provider.
    assert s.search_provider == "auto"
    assert s.mcp_peers == ""  # no peer agents until configured
    assert s.max_tool_iterations == 5


def test_the_brain_is_configured_by_tier_not_model_id():
    """Model choice moved behind agent_runtime's router, so one edit there moves
    every app in the ecosystem instead of one edit per app."""
    s = _fresh()
    assert s.llm_tier == "balanced"
    assert s.memory_tier == "balanced"
    assert not hasattr(s, "llm_model")


def test_openclaw_is_gone():
    """The bridge was replaced by peer MCP servers; leftover keys would be a
    misleading thing to find in a .env."""
    s = _fresh()
    for removed in ("openclaw_url", "openclaw_token", "openclaw_timeout_s"):
        assert not hasattr(s, removed)


def test_native_search_config_is_gone_with_the_server_tool():
    """`search_tool_version`/`search_max_uses` only ever configured Anthropic's
    native server tool. Leaving them would imply a knob that does nothing."""
    s = _fresh()
    for removed in ("search_tool_version", "search_max_uses"):
        assert not hasattr(s, removed)


def test_turn_based_defaults():
    s = _fresh()
    assert s.feature_turn_based is True
    assert s.feature_client_tools is True
    assert s.recall_messages == 12
    # Cold-start recap was bumped for richer cross-session continuity.
    assert s.recent_recap_messages == 8


def test_auth_enabled_follows_secret():
    assert _fresh(auth_secret="").auth_enabled is False
    assert _fresh(auth_secret="hunter2").auth_enabled is True


def test_the_mcp_server_needs_keys_as_well_as_the_flag():
    """agent-mcp-py fails closed and refuses to build an unauthenticated app, so
    without keys Amber must decline to mount rather than crash at startup."""
    assert _fresh().mcp_server_enabled is False  # flag on, no keys
    assert _fresh(mcp_keys="  ").mcp_server_enabled is False
    assert _fresh(mcp_keys="spawner:tok").mcp_server_enabled is True
    assert (
        _fresh(mcp_keys="spawner:tok", feature_mcp_server=False).mcp_server_enabled
        is False
    )


def test_get_settings_is_cached():
    config_module.get_settings.cache_clear()
    a = config_module.get_settings()
    b = config_module.get_settings()
    assert a is b
