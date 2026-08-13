"""Central configuration.

Every model choice, API key, and feature flag flows through here so the brain,
STT, and TTS are swappable without touching call sites. Nothing in the codebase
should hardcode a model name or key — import `settings` instead.

Values are read from environment variables (prefix ``AMBER_``) and an optional
`.env` file. See `.env.example` for the full list.

**One prefix, three consumers.** Amber now embeds two shared libraries
(`agent_runtime`, `agent_mcp`) that can each read their own ``AGENT_RUNTIME_*`` /
``AGENT_MCP_*`` environment. Amber does not use that: both are configured by
*injection* from the values below, so there is a single place to look and no
possibility of two prefixes disagreeing about, say, which database to write. See
`app.brain` and `app.mcp_server` for where the settings objects are built.
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
    openrouter_api_key: str = Field(
        default="",
        description=(
            "OpenRouter API key for the LLM brain, used via agent-runtime. "
            "Replaces the direct Anthropic key: the brain now reaches every "
            "provider through one endpoint and a named tier."
        ),
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
    # The brain, as a **named tier** rather than a model id. agent_runtime's
    # model_router resolves "balanced" -> a concrete model, so upgrading every app
    # in the ecosystem is one edit in the router rather than one per app. Today
    # "balanced" is Claude Haiku, which is what Amber used before this indirection
    # existed — fast and low-latency, which is what a voice loop needs. A literal
    # model id containing "/" still passes through as an escape hatch.
    llm_tier: str = "balanced"
    # Cap on a single spoken reply. Voice answers are short; keep this modest so
    # a runaway generation can't stream for minutes. Bump it for longer replies.
    llm_max_tokens: int = 1024
    # The memory writer's fact-extraction model (Phase 3). Off the latency path, so
    # it could be a beefier tier; defaults to the brain's for cost/simplicity.
    memory_tier: str = "balanced"
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
    # Peer MCP servers Amber may call as a client, as "name=https://host" pairs.
    # Heavy or delegated work goes here now that the OpenClaw bridge is gone — the
    # inline-vs-delegated distinction survives, only the far end changed. Empty
    # means Amber has no peers and uses inline tools only.
    mcp_peers: str = ""
    mcp_peer_token: str = ""

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
    # When false, Amber's own MCP server is not mounted. She stays a *caller* of
    # other agents either way; this only controls whether she is queryable herself.
    feature_mcp_server: bool = True

    # --- Amber's own MCP server ---
    # Comma-separated bearer tokens other agents present to query Amber, each
    # optionally "name:token" so the usage log records who called. agent-mcp-py
    # fails closed, so no keys means the server is simply not mounted rather than
    # mounted wide open — see `mcp_server_enabled`.
    mcp_keys: str = ""
    # Amber's externally reachable base URL, no /mcp suffix. Needed only to
    # register with the hosted sync store; she serves normally without it.
    mcp_public_url: str = ""
    # The hosted discovery store. Empty disables registration, and an unreachable
    # one never blocks startup.
    mcp_sync_store_url: str = ""
    mcp_sync_store_token: str = ""

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

    @property
    def mcp_server_enabled(self) -> bool:
        """True if Amber should mount her own MCP server.

        Requires keys as well as the flag. agent-mcp-py fails closed and refuses to
        build an unauthenticated app, so without this check a default install would
        crash at startup rather than simply not exposing a server nobody asked for.
        """
        return self.feature_mcp_server and bool(self.mcp_keys.strip())


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the `.env` file is parsed once. Tests can clear the cache with
    ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()


# Convenience handle for call sites that don't need lazy loading.
settings = get_settings()
