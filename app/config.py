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


#: What this app calls itself to the rest of the ecosystem.
#:
#: Not a setting — it is the name Amber registers under, the prefix on the tools she
#: exposes to peers, and therefore the one name she must never resolve as a peer
#: *herself*: ``GET /servers`` returns her own registration, so an install that
#: discovers anything also discovers itself unless it knows its own name.
#:
#: Here rather than in `app.mcp_server` because `app.peers` needs it and sits on the
#: prompt path — importing the MCP server from there would drag `app.tools` and the
#: memory store in behind it, to learn a five-letter string. Config is the one module
#: both already import, and it imports nothing of Amber's.
APP_NAME = "amber"


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
    # The default voice. Every field here is the *install default* — a client may
    # override any of them for its own connection with a ``set_voice`` frame (see
    # `app.voice`), which is what lets a desktop app expose them as UI instead of
    # requiring an edit on the box and a restart.
    #
    # ``tts-1`` is the low-latency model and the one ``tts_speed`` acts on directly.
    # ``gpt-4o-mini-tts`` sounds considerably better and takes prose direction via
    # ``tts_instructions``, but ignores ``speed`` — `app.voice` translates a speed
    # into a pacing instruction there so the knob means one thing on both.
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_format: str = "mp3"
    # Speaking rate, 0.25–4.0. The endpoint's own default is 1.0, which is a
    # measured, audiobook pace — fine for narration and noticeably slow for
    # conversation, where the listener already has the context. 1.15 is the mild
    # correction; set it back to 1.0 for the API's own behaviour.
    tts_speed: float = 1.15
    # Free-text direction for the voice ("warm, brisk, dry"). Only ``gpt-4o-mini-tts``
    # acts on it; it is not sent to the older models, which reject it.
    tts_instructions: str = ""
    # The brain, as a **keyword** rather than a model id — you say what you want the
    # model to be like ("fast", "coding", "strong") and `app/models.py` resolves it.
    # This is the *default*: a connection can pick another keyword for itself with a
    # ``set_model`` frame, and the keyword table itself is re-pointable at runtime
    # (see app/models.py). A literal model id containing "/" still passes through
    # untouched as an escape hatch.
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
    # When false, ``set_voice`` frames are ignored and every connection speaks with
    # the ``tts_*`` settings above. The override is per-connection and can only pick
    # values from `app.voice`'s catalogue, so this is a preference (pin the voice for
    # every client) rather than a security boundary. The ``voice`` frame is still
    # sent either way, so a client can show what it is getting.
    feature_voice_control: bool = True
    # When false, ``set_model`` frames are ignored: every connection uses ``llm_tier``
    # and the keyword table can't be re-pointed over the wire (the ``.env`` and the
    # database still decide it). Like voice control this is a preference, not a
    # security boundary — a client can only ever name a model, and the ``model`` frame
    # is still sent so it can show what it is getting, marked ``locked``.
    feature_model_control: bool = True
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
    # When true, every system prompt carries a short description of the ecosystem
    # Amber belongs to — Aperture, the shared libraries, the sync store, Bloom (see
    # app/ecosystem.py). On by default: she is the natural-language way into all of
    # it, and without this she answers questions about her own system by guessing or
    # searching the web for something private. Turn it off for a deployment that
    # wants a plain voice assistant and would rather not pay the tokens every turn.
    feature_ecosystem_context: bool = True
    # When true, the notes the maintenance pass writes about itself are injected
    # into every system prompt. Default off: reflections are worth *reading* long
    # before they're worth acting on automatically, and this is the line between
    # "Amber notices patterns" and "Amber edits her own instructions". Turn it on
    # once a few real reflections have been read and judged sane.
    feature_self_notes: bool = False
    # Turn visibility: when true, every tool call is reported to the client as it
    # starts and again as it finishes (the additive ``activity`` frame), so a UI can
    # show what a turn is *doing* rather than a spinner. Advisory — nothing here
    # feeds back into the turn — and off means the frames are simply never sent,
    # which is exactly how every client behaved before they existed.
    feature_activity_stream: bool = True
    # When true, raw reply text is streamed as it is generated (the additive
    # ``delta`` frame) alongside the sentence-by-sentence audio path. Sentences are
    # the *speech* view of a reply and lose the model's own whitespace, so a client
    # that wants to render markdown needs this one. Off costs nothing but flat text.
    feature_text_stream: bool = True
    # When true, a client may forget, restore and correct remembered facts, and browse
    # everything Amber knows (the ``memory_action`` / ``memory_query`` frames). The
    # model-facing half of this — the ``forget_fact`` / ``correct_fact`` tools — has
    # always existed, so off leaves memory curatable by asking but not by clicking.
    # Deletion is soft either way, so a mistake here is recoverable.
    feature_memory_control: bool = True

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
    # one never blocks startup. It also holds the ecosystem's shared model-keyword
    # table (see feature_model_sync below and app/model_sync.py).
    mcp_sync_store_url: str = ""
    mcp_sync_store_token: str = ""

    # --- Shared model keywords ---
    # Push Amber's keyword overrides to the sync store and pull everyone else's, so
    # "coding" means the same model in every app. Requires mcp_sync_store_url; with
    # no store configured this does nothing and the table is simply local. Off means
    # Amber keeps her own table and never talks to the store about it.
    feature_model_sync: bool = True
    # How often the reconciliation pass runs. Keyword changes are rare and a client
    # edit pushes immediately, so this is the catch-up for changes made elsewhere.
    # Floored at 30s in the loop.
    model_sync_interval_s: float = 300.0

    # --- Peer discovery ---
    # Resolve peers through the sync store's registry, not just ``mcp_peers``.
    #
    # The bug this closes. ``app.brain.build_broker`` used to build its MCP client
    # from ``load_static_peers(mcp_peers)`` alone and pass that same dict as the
    # ``resolver`` — which short-circuits ``agent_mcp.registry.resolve`` entirely.
    # So with an empty ``AMBER_MCP_PEERS`` no client was constructed at all, and a
    # peer could register perfectly, appear in ``GET /servers``, and be offered to
    # the model as nothing whatsoever. There is no error in that state: an unlisted
    # peer is not a failing tool, it is no tool.
    #
    # The store has always held a credential per server, settable only by an admin
    # and handed out on discovery, and `agent_mcp.PeerRegistry` has always kept a
    # two-layer cache with static overriding discovered. None of it was reachable
    # from here. This flag turns the second layer on.
    #
    # On by default, because "the peer I registered is callable" is the behaviour
    # everything else in the ecosystem already assumes. Off pins Amber to the static
    # map — the switch to reach for when a bad registry entry is sending her
    # somewhere she should not go, since static always wins anyway.
    feature_peer_discovery: bool = True
    # How often the peer list is pulled. Matches agent_mcp's own 300s registration
    # heartbeat, so a peer that has just registered is discoverable within about one
    # of its own cycles. Floored at 30s in the loop.
    peer_sync_interval_s: float = 300.0

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
