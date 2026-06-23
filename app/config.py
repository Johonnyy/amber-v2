"""Central configuration.

Every model choice, API key, and feature flag flows through here so the brain
(Phase 2), STT, and TTS are swappable without touching call sites. Nothing in the
codebase should hardcode a model name or key — import `settings` instead.

Values are read from environment variables (prefix ``AMBER_``) and an optional
`.env` file. See `.env.example` for the full list.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AMBER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Secrets ---
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key used for both STT (Whisper) and TTS.",
    )
    anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key used for the LLM brain (Claude).",
    )

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # --- Models (swappable) ---
    stt_model: str = "whisper-1"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_format: str = "mp3"
    # The brain (Claude Haiku — fast, low-latency, good for a voice loop).
    llm_model: str = "claude-haiku-4-5-20251001"
    # Cap on a single spoken reply. Voice answers are short; keep this modest so
    # a runaway generation can't stream for minutes. Bump it for longer replies.
    llm_max_tokens: int = 1024
    # The memory writer's fact-extraction model (Phase 3). Off the latency path, so
    # it could be a beefier model; defaults to the brain's for cost/simplicity.
    memory_model: str = "claude-haiku-4-5-20251001"
    # Token cap for one fact-extraction call. Output is a short JSON list; keep low.
    memory_extract_max_tokens: int = 512

    # --- Memory (Phase 3) ---
    # SQLite file holding persistent cross-session knowledge (facts/conversations/
    # tasks). Distinct from per-connection history (see app/session.py).
    memory_db_path: str = "amber.db"
    # Hard cap on facts injected into the system prompt per turn. Memory is paid for
    # in tokens on every call — keep it small and high-signal.
    memory_max_facts: int = 12
    # Hard cap on new facts kept from a single exchange, so one turn can't flood
    # the store.
    memory_max_new_facts: int = 5

    # --- Tools (Phase 4) ---
    # Max tool-use round trips the brain will make in one turn before it must
    # answer with what it has. A backstop against a model that loops on tools.
    max_tool_iterations: int = 5
    # Web search (inline tool). Provider selects the backend:
    #   "duckduckgo" — keyless Instant Answer API (default; quick factual lookups)
    #   "tavily"     — LLM-oriented search; requires search_api_key.
    search_provider: str = "duckduckgo"
    search_api_key: str = ""
    # Hard cap on result snippets folded into one tool result (kept small — the
    # model pays for them in tokens, and voice answers are short).
    search_max_results: int = 3
    search_timeout_s: float = 10.0
    # OpenClaw bridge — heavy/delegated work over HTTP to the separate OpenClaw
    # service. The tool is only offered to the model when a URL is configured.
    # ``openclaw_url`` is the host/gateway (e.g. "https://10.0.0.5:8080"),
    # ``openclaw_token`` the gateway/bearer token sent as Authorization.
    openclaw_url: str = ""
    openclaw_token: str = ""
    # OpenClaw work can be slow (browser, multi-step); allow a generous timeout —
    # the bridge blocks the response until it returns.
    openclaw_timeout_s: float = 60.0
    # ``update_server`` tool — runs the deploy update script on the box. The tool is
    # only offered to the model when this command is set (empty = hidden), since it
    # is a privileged, server-mutating action. The command is run through a shell.
    # NOTE: the script restarts the amber service; configure it to run detached from
    # the service cgroup (e.g. via ``systemd-run``) so the restart doesn't kill the
    # update mid-flight — see .env.example.
    update_command: str = ""
    # How long to wait for the update command before giving up and returning to the
    # model (the detached script keeps running regardless).
    update_timeout_s: float = 120.0
    # Client-provided tools (see app/client_tools.py). A client may declare tools it
    # can run on its own device (text display, sounds, ...); Amber offers them to the
    # model prefixed with ``client_`` and dispatches calls back over the WS.
    # Hard cap on tools one client may register, to bound prompt token cost.
    max_client_tools: int = 16
    # How long the brain waits for a client to return a tool result before giving up
    # on that call and telling the model it failed.
    client_tool_timeout_s: float = 30.0

    # --- Feature flags ---
    feature_stt: bool = True
    # When false, the LLM brain is bypassed and the Phase-1 canned greeting is
    # returned instead. Lets the pipe run without an Anthropic key (tests, demos).
    feature_llm: bool = True
    # When false, no memory is read into the prompt and no facts are extracted —
    # the pipeline behaves exactly as Phase 2. Lets the loop run with no DB.
    feature_memory: bool = True
    # When false, the brain never offers tools to the model — it streams a direct
    # reply exactly as Phase 2/3. Lets the loop run without any tool plumbing.
    feature_tools: bool = True
    # When false, client-declared tools are ignored — the brain never offers them
    # and never calls back to the client. Independent of ``feature_tools`` (which
    # governs Amber's own server-side tools).
    feature_client_tools: bool = True

    # --- Sessions (Phase 5) ---
    # How long an idle session's in-memory history is retained for reconnect/
    # resume, in seconds. A client that reconnects with its id inside this window
    # picks up where it left off; after it, the id starts a fresh session.
    session_ttl_s: float = 1800.0  # 30 minutes
    # Hard cap on concurrently retained sessions (memory guardrail). Past this the
    # least-recently-active sessions are evicted. 0 disables the cap.
    max_sessions: int = 1000
    # Cap on conversation *turns* (user+assistant pairs) kept in a session's
    # in-memory history. Older turns drop off so the context window — and the
    # tokens every turn pays for it — stays bounded. 0 disables trimming.
    max_history_turns: int = 50

    # --- Rate limiting & cost guardrails (Phase 5) ---
    # Max utterances processed per session within the rolling window. Protects the
    # STT/LLM/TTS spend from a stuck or abusive client. 0 disables.
    rate_limit_turns: int = 30
    rate_limit_window_s: float = 60.0
    # Reject an inbound utterance larger than this (bytes) before spending STT. 0
    # disables the size check.
    max_audio_bytes: int = 10 * 1024 * 1024  # 10 MB
    # Hard cap on total utterances over a single session's lifetime (cost
    # guardrail). 0 disables.
    max_turns_per_session: int = 0

    # --- Auth (Phase 5; disabled when empty) ---
    # Shared secret clients present to connect: as ``?token=`` on the WS URL, or an
    # ``Authorization: Bearer <secret>`` header. Empty = auth off (open socket).
    auth_secret: str = ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_secret)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the `.env` file is parsed once. Tests can clear the cache with
    ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()


# Convenience handle for call sites that don't need lazy loading.
settings = get_settings()
