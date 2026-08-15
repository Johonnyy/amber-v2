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
    # IANA timezone the brain's clock is read in (e.g. "America/New_York"). Drives
    # the date/time line injected into every system prompt (app/runtime_context.py).
    # An unknown/unavailable zone degrades to UTC rather than failing a turn.
    timezone: str = "UTC"

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
    # On a *cold* turn (the first of a fresh/reconnected session, when the live
    # in-memory history is empty), how many of the most recent durable messages to
    # replay into the prompt as a "where you left off" recap — cross-session
    # continuity the live history can't provide yet. Skipped once the session has
    # its own history (which already covers recent context). 0 disables the recap.
    recent_recap_messages: int = 8
    # How many recent durable messages the ``recall_recent`` tool returns when the
    # user refers back to an earlier conversation (turn-based conversations). Off the
    # per-turn path — paid only on turns where the model actually calls the tool —
    # so it can be larger than the always-on recap above.
    recall_messages: int = 12

    # --- Memory retrieval & lifecycle ---
    # Character budget for the memory block in the system prompt. The facts cap
    # above counts rows; this one counts what they actually cost, since twelve long
    # facts are far more expensive than twelve short ones and every turn pays it.
    memory_max_chars: int = 1200
    # How many candidate facts the ranker pulls from the index before scoring. Kept
    # well above memory_max_facts so scoring has something to choose between, and
    # well below the table size so the read never degrades into a full scan.
    memory_candidates: int = 40
    # Most-used durable facts always included regardless of relevance to this turn.
    # Identity-level knowledge (who he is, where he lives, how he likes answers)
    # rarely shares words with the question, so pure relevance ranking drops it.
    memory_always_durable: int = 5
    # Cap on open tasks surfaced in the memory block.
    memory_max_tasks: int = 8
    # Fact lifecycle, applied by the maintenance pass (app/maintenance.py):
    # a short-tier fact used fewer than twice and untouched for this long is
    # forgotten; a session-tier one goes much sooner; durable facts never decay.
    fact_short_ttl_days: float = 30.0
    fact_session_ttl_hours: float = 12.0
    # Uses at which a short-tier fact is promoted to durable. Being repeatedly
    # useful is the only evidence of durability that doesn't need a model.
    fact_promote_uses: int = 3
    # Retention for the raw exchange log. It is the substrate for recall and
    # re-distillation, not a permanent archive.
    conversation_keep_days: float = 180.0

    # --- Maintenance (the self-improvement loop) ---
    # How long after startup the first pass runs. Not cosmetic: the test suite spins
    # the app up repeatedly, and without a delay every one of those would kick off a
    # maintenance pass against the real amber.db.
    maintenance_startup_delay_s: float = 300.0
    maintenance_interval_s: float = 21600.0  # 6 hours
    # The model that consolidates facts and writes reflections. Off the latency
    # path, so it could be a stronger tier than the brain.
    maintenance_tier: str = "balanced"
    maintenance_max_tokens: int = 1024
    # Facts handed to one consolidation call. Bounds both cost and blast radius —
    # the pass looks at what changed since last time, never the whole store.
    maintenance_max_facts: int = 60
    # Hard cap on mutations applied from one model response. A confused model
    # should be able to make a mess of at most this many facts.
    maintenance_max_changes: int = 20
    # Reflections written per pass, and how many are kept in view afterwards.
    maintenance_max_reflections: int = 3
    # Retention for telemetry rows.
    signals_keep_days: float = 30.0

    # --- Tools (Phase 4) ---
    # Max tool-use round trips the brain will make in one turn before it must
    # answer with what it has. A backstop against a model that loops on tools.
    max_tool_iterations: int = 5
    # Web search. The provider selects the backend:
    #   "auto"       — tavily when search_api_key is set, duckduckgo when it isn't
    #                  (default): the good backend when available, a working one
    #                  otherwise, no configuration required either way.
    #   "tavily"     — LLM-oriented search API; needs search_api_key. Asking for it
    #                  explicitly without a key is an error, not a silent downgrade.
    #   "duckduckgo" — keyless. Instant Answers alone answers almost nothing, so it
    #                  falls back to scraping the HTML results page — best-effort,
    #                  and it will break when their markup changes.
    #
    # There used to be a third, "anthropic": a *native server-side* tool Anthropic
    # ran inside the LLM request, which was the default and was far better for
    # current events. It went when the brain moved to `agent_runtime` — a server
    # tool lives inside the provider's own request loop, and the OpenAI-compatible
    # endpoint has no equivalent. Tavily is the closest replacement, which is why
    # "auto" prefers it: set AMBER_SEARCH_API_KEY to get comparable quality back.
    search_provider: str = "auto"
    search_api_key: str = ""
    # Hard cap on result snippets folded into one tool result. Raised from 3 now
    # that results carry URLs and a snippet is a lead for read_url, not just an
    # answer — but still small, since the model pays for them in tokens.
    search_max_results: int = 5
    search_timeout_s: float = 10.0
    # ``read_url``: fetch one page and hand the model its readable text.
    read_url_timeout_s: float = 15.0
    # Hard cap on the download, enforced while streaming rather than by trusting
    # Content-Length — a missing or lying header must not pull an unbounded body
    # into memory.
    read_url_max_bytes: int = 2 * 1024 * 1024  # 2 MB
    # Characters of extracted text handed to the model. A long article costs real
    # tokens on every later turn of the exchange; the head of it is nearly always
    # where the answer is.
    read_url_max_chars: int = 4000
    # Peer MCP servers Amber may call as a client, as "name=https://host" pairs.
    # Heavy or delegated work goes here now that the OpenClaw bridge is gone — the
    # inline-vs-delegated distinction survives, only the far end changed. Empty
    # means Amber has no peers and uses inline tools only.
    mcp_peers: str = ""
    mcp_peer_token: str = ""
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
    # Turn-based conversations: when true, the brain offers the ``expect_reply``
    # signaling tool so Amber can deliberately hold a turn open for the user's
    # answer (``turn_complete`` then carries ``awaiting_response``). When false the
    # tool is never advertised and the field is never sent — identical to before.
    feature_turn_based: bool = True
    # When false, Amber's own MCP server is not mounted. She stays a *caller* of
    # other agents either way; this only controls whether she is queryable herself.
    feature_mcp_server: bool = True
    # Telemetry: tool outcomes, corrections, barge-ins, per-turn shape. This is the
    # raw material the maintenance pass reflects on — with it off, memory hygiene
    # still runs but Amber notices nothing about how conversations are going.
    feature_signals: bool = True
    # The periodic self-maintenance pass (app/maintenance.py): decay, promotion,
    # consolidation, pruning, self-review. Off means memory never curates itself.
    feature_maintenance: bool = True
    # When true, the notes the maintenance pass writes about itself are injected
    # into every system prompt. Default off: reflections are worth *reading* long
    # before they're worth acting on automatically, and this is the line between
    # "Amber notices patterns" and "Amber edits her own instructions". Turn it on
    # once a few real reflections have been read and judged sane.
    feature_self_notes: bool = False

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
