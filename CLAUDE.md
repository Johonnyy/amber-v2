# CLAUDE.md — The Amber Ecosystem

This file describes the whole project: what it is, how the pieces fit together, and
the conventions every repo in the ecosystem follows. Copy the relevant sections into
a repo-specific CLAUDE.md when working inside an individual app; this version is the
canonical, ecosystem-wide reference.

It also documents **this** repo (`amber`) in detail — see [This repo:
Amber](#this-repo-amber) near the bottom. Amber is built and working; the ecosystem
around her is mostly not built yet. Keep the two clearly separated when reading.

## What this is

A personal, open-source ecosystem of independent apps (finance, school, project
tracking, FreeCallMe's dashboard, etc.), each usable completely standalone, that also
expose themselves to a personal AI layer via MCP. **Amber** is the orchestrating
voice/text agent that knows Johnny and can query or act across every connected app.
**Aperture** is the unifying Electron shell that ties the apps together visually and
manages device config/sync. Every app can be cloned and run alone by anyone — the
agent layer is always an opt-in extension, never a dependency.

## Core principle (do not violate)

**Every app must run standalone with zero knowledge the ecosystem exists.** If `git
clone`-ing a single app and running it requires anything from another repo, that's a
bug in the design. The agent/MCP layer is always optional, toggled by a feature flag,
off by default.

## The reframe that resolved most early confusion

Two different things were being conflated: **the mechanism** that lets an LLM call
tools and loop (build once, shared) vs. **the specific tools/data each app exposes**
(unique per app, but standardized via protocol). MCP is the shared protocol.
`agent-runtime` is the shared mechanism (a library). Every app just needs to expose
*its own* data/actions as MCP tools and resources — it does not need its own copy of
the loop logic.

## Naming reference

| Name | What it is |
|---|---|
| **Amber** | Personal orchestrating agent — voice pipeline, memory, backend-only, no frontend |
| **Aperture** | Electron shell app — unifying UI, device-local config store, import/export/sync |
| **agent-spawner** | Service wrapping `agent-runtime` — decides model tier, routes tasks, tracks cost centrally |
| **notification-relay** | Push notification fan-out (Redis pub/sub → APNs → iOS) |
| **finance-agent, school-agent, outpost, freecallme, etc.** | Individual domain apps, each with their own frontend + MCP server |

Naming style for future apps: single clean nouns, consistent with
Outpost/ThinkTank/Aperture (Forge, Sentinel, Herald, Atlas, etc. — see naming
brainstorm history for the full candidate list, check for collisions before
assigning).

## Repo list

- `amber` — the agent itself (**this repo**; exists and works, needs refactor — see below)
- `agent-mcp-py` — shared library: wraps the MCP Python SDK with auth, depth-guard,
  usage logging, sync-store registration. Every Python app's MCP server is built on this.
- `agent-runtime` — shared library: the actual agentic loop (call model → tool call →
  execute → repeat), built on OpenRouter's OpenAI-compatible endpoint. Imported
  directly (not called over network) by Amber and agent-spawner.
- `agent-spawner` — service, imports `agent-runtime` in-process, exposes task
  delegation as an MCP tool for apps that don't want to embed the runtime themselves.
- `notification-relay` — service, Redis pub/sub, single `send_notification` endpoint
  (also exposed as an MCP tool).
- `amber-infra` — deployment backbone: Caddy config, install script, backup scripts,
  CI templates, the hosted config sync store.
- `amber-template` — scaffold repo, pre-wired with `agent-mcp-py`, Docker, CI,
  backups. `npx degit` starting point for every new app.
- `Aperture` — Electron shell.
- Individual app repos (`finance-agent`, `outpost`, etc.) — each standalone, each
  optionally MCP-enabled.
- `freecallme` — existing Next.js/Vercel/Supabase app, **not rewritten**; gets a small
  TypeScript MCP sidecar added.

## Tech stack decisions

- **New backend-heavy apps default to Python (FastAPI)** — this is what lets them
  share `agent-mcp-py` and `agent-runtime` with Amber and the spawner.
- **Frontends default to Next.js (React)** for anything with a dashboard.
- **Existing apps are not rewritten to match the pattern.** FreeCallMe stays Next.js;
  it gets an MCP server written in TypeScript (`@modelcontextprotocol/sdk`) as a
  sidecar, not a port to Python. MCP is the interop layer specifically so language
  doesn't have to match everywhere — only the protocol does.
- **Aperture** is Electron + React, frontend-only, not itself an MCP server.
- **Registry / service discovery** is not a static YAML file — it's a small hosted sync
  store (living alongside `notification-relay` or similar always-on service) that
  Aperture edits through a UI and every headless agent (Amber, spawner, apps) reads
  directly. Aperture's on-device storage is a cache + what enables import/export and
  multi-device sync; it is never the only copy, since headless services must work even
  when Aperture isn't open.

## Conventions every app must follow

- **Resource URIs mirror real dashboard views.** If a screen shows data, there's a
  matching MCP resource returning the same data (e.g. `finance://transactions/recent`).
  No separate "agent-only" version of the data.
- **Tools mirror real user actions.** If a human can click it, there's a tool that does
  the same thing, calling the same underlying function as the UI — not a parallel code
  path that can drift.
- **Query tools are marked `read_only=True`.** Action tools that are risky get
  `requires_confirmation=True`, gated by an `X-Confirmed` header set only after
  explicit approval.
- **Conversation depth is tracked via `X-Conversation-Id` and `X-Agent-Depth`
  headers**, capped at 5 hops, to prevent agent-to-agent call loops.
  `agent_mcp.call_peer()` handles threading these automatically for any server-to-server
  composition (e.g. FreeCallMe's MCP server calling PostHog's).
- **External integrations (Stripe, PostHog, etc.) are composed, not re-implemented.** An
  app's own MCP server acts as a client to third-party MCP servers internally and
  re-exposes clean, domain-specific tools on top.
- **Usage and cost logging stay local to each app's own DB** — no shared central
  database, consistent with the independence principle. The spawner aggregates cost
  views by querying each app's own usage summary, not a shared table.
- **Model selection uses named tiers** (`cheap` / `balanced` / `strong`) resolved
  through `agent_runtime.model_router`, never hardcoded model strings scattered across
  apps. Update the tier table in one place as better/cheaper models appear.

## How agents improve over time

- Prompts and tool definitions are versioned in git like any other code change.
- Every tool call is logged (caller, latency, success, conversation_id) via
  `agent_mcp`'s usage logging.
- Lightweight thumbs up/down feedback, tagged to conversation_id, surfaces through
  Aperture once it exists.
- The model-tier routing table gets refined from real cost/quality data over time, not
  fixed upfront.
- Small per-app eval sets (10-20 hand-written query → expected-tool-call cases) catch
  regressions when prompts or models change.

## Deployment

**Target state:** two OVH servers. Server A (core/always-on): Amber, agent-spawner,
notification-relay, config sync store, Caddy. Server B (apps): individual app agents,
also behind Caddy. Every subdomain (`amber.johnny.dev`, `finance.johnny.dev`, etc.)
routes via one Caddy instance per server with per-app config snippets. Docker Compose
per app, pinned image tags (never `latest`), Watchtower for auto-updates initially,
migrating core services to GitHub Actions webhook deploys once stable for tighter
control over deploy timing.

**Actual state today:** Amber alone, on one OVH VPS, as a bare systemd unit — no
Docker, no Caddy, no second server. See [deploy/README.md](deploy/README.md).
Containerizing Amber is part of the `amber-infra` work, not done yet.

---

# This repo: Amber

Everything below describes code that exists in this repository right now.

## What Amber is

A cloud-hosted personal AI backend — a persistent, always-available voice agent with
no UI of its own. Everything the user touches (an earpiece, a Pi with a screen, a
browser tab) is a thin **client**; Amber is the intelligence behind all of them.

It is **not** a chatbot UI, not a local model, not a configurable framework. It is a
codebase you own.

## Core architecture

### The voice loop (the entire contract)

```
client records audio → WS → Amber transcribes (Whisper/OpenAI STT)
  → Amber thinks (LLM call, with memory injected as system context)
  → LLM stream → sentence splitter → TTS (OpenAI) → WS → client plays
```

The client only records and plays. Audio streams back **sentence by sentence** — the
sentence splitter sits between the LLM token stream and TTS so the first audio plays
before the full response is generated. This streaming boundary is the
performance-critical seam; keep it intact when modifying the pipeline. The pending
`agent-runtime` swap changes only what produces the token stream, never this seam.

### Client protocol (WebSocket)

Every client speaks the same protocol — building a new client means writing a thin
wrapper around it:
- **Send:** raw audio, or a `user_text` frame when the client already has the words.
- **Receive:** streamed audio + optional metadata (transcript, thinking state, tool events).
- **Interrupt:** client sends an interrupt message → Amber stops speaking mid-response.

Treat this protocol as a stable public contract. Changing message shapes breaks every
client; additive changes only.

**Typed turns.** `user_text` (`{"type": "user_text", "text": "..."}`) is the exact peer
of a binary utterance: it takes a turn slot, obeys the same rate limit and session cap,
and barges in on an in-flight turn identically. The *only* difference is that STT is
skipped — `run_turn(audio=None, ..., text=...)` takes the words verbatim, and everything
from the `transcript` echo onward is the same code path a spoken turn takes, reply
audio included. Guardrails split accordingly: `_admit_utterance` keeps the audio-only
size check and delegates the shared ones to `_admit_turn`. Blank or non-string text is
ignored silently, like any other malformed control frame. This exists because a desktop
client needs typed input to be a usable debugging surface; voice-only clients are
unaffected.

### Memory

Amber builds a persistent picture of the user across all conversations — facts,
preferences, ongoing tasks, noticed patterns. Stored in SQLite.

Two halves:
- **Writer:** after each exchange, extract distilled facts worth keeping. Store *punchy
  distilled facts, not raw transcripts.*
- **Context builder:** on each new message, pull relevant memory into the system prompt
  as a **compressed** block.

Keep memory small and high-signal — every LLM call pays for it in tokens. Bloating it
degrades both cost and quality.

### Session model

Conversation history is maintained **per WebSocket connection, in-memory** (retained
briefly across reconnects by the session manager). Persistent cross-session knowledge
lives only in memory (SQLite), not in conversation history. Don't conflate the two.

## Config

All model choices, API keys, and feature flags go through `app/config.py` (env prefix
`AMBER_`, plus `.env`). Don't hardcode model names or keys inline — route them through
config so the brain, STT, and TTS models are swappable. Brain model choice is a
**named tier** (`llm_tier`) resolved by `agent_runtime.model_router`, not a literal
model id.

**One prefix, three consumers.** Amber embeds `agent_runtime` and `agent_mcp`, both
of which can read their own `AGENT_RUNTIME_*` / `AGENT_MCP_*` environment. Amber uses
neither: `app.brain.runtime_settings` and `app.mcp_server.mcp_settings` construct
their settings objects from the values here with `_env_file=None`. Add a knob to
`app/config.py` and inject it — never a second prefix in `.env`.

## Commands

Stack: FastAPI + WebSocket server, Python 3.11+. Source in `app/`, tests in `tests/`.

```bash
# Setup (from repo root)
python -m venv .venv
.venv/Scripts/activate            # Windows;  .venv/bin/activate on Linux/macOS
pip install -e ".[dev]"           # runtime + dev deps from pyproject.toml
cp .env.example .env              # then set AMBER_OPENAI_API_KEY

# Run the server locally (hot reload)
uvicorn app.main:app --reload     # health: GET /health ; voice: ws://127.0.0.1:8000/ws

# Tests (no network / API key needed — STT & TTS are faked in tests)
pytest                            # whole suite
pytest tests/test_sentence_splitter.py                       # one file
pytest tests/test_pipeline.py::test_run_turn_streams_sentence_audio   # one test

# Prove the pipe end to end against a running server (needs a real API key)
python scripts/smoke_client.py path/to/utterance.wav
```

Deploy to the OVH VPS via systemd — see [deploy/README.md](deploy/README.md). The unit
is `deploy/amber.service`; runs `uvicorn app.main:app` under user `amber` from
`/opt/amber`, config from `/opt/amber/.env`. Update: `git pull` then `systemctl restart
amber`.

Key modules: [app/config.py](app/config.py) (all tiers/keys/flags),
[app/protocol.py](app/protocol.py) (WS wire contract),
[app/sentence_splitter.py](app/sentence_splitter.py) (streaming seam),
[app/pipeline.py](app/pipeline.py) (the voice loop), [app/main.py](app/main.py)
(FastAPI + `/ws` + `/mcp` + `/agent/usage`, and the lifespan that starts the signal
writer and the maintenance loop). The "brain" is
[app/brain.py](app/brain.py) — a thin wrapper over `agent_runtime.AgentRunner` —
with its personality in [app/persona.py](app/persona.py) (`compose_system_prompt`
composes the core, the modality block, the device block, and the per-turn context);
[app/signals.py](app/signals.py) and [app/maintenance.py](app/maintenance.py) are
the self-improvement loop; [app/session.py](app/session.py) holds per-connection
conversation history. [app/responder.py](app/responder.py) is the canned fallback
used when `AMBER_FEATURE_LLM=false`. Both speak the same `AsyncIterator[str]`
contract, so the pipeline downstream of the brain is unchanged.

The system prompt is **composed, not monolithic** ([app/persona.py](app/persona.py)).
`compose_system_prompt` is keyword-only and layers a fixed `CORE` (identity, how to
answer, tools, memory posture) with blocks that only apply to this turn: a
**modality** block (`SPOKEN_STYLE` for audio turns, `TYPED_STYLE` for `user_text`
ones — the pipeline derives it from `text is not None`), a **device** block naming
the client tools this connection actually declared, and the per-turn context blocks.
That structure exists because one flat prompt drifted badly: it named a deleted
backend three times, banned emoji then required one, described display tools a
headless client never had, and gave "spell it for the ear" rules to typed replies.
A block that's only emitted when it applies can't rot the same way. `SYSTEM_PROMPT`
is `compose_system_prompt()` with no context — the brain's default.

Per-turn context. `app/runtime_context.py` (`build_runtime_context`) is a one-line
date/time stamp read in `AMBER_TIMEZONE` (unknown zone → UTC), *always* injected
independent of the memory flag; then the memory block. The conversation history
downstream supplies "what was just said". The core prompt tells Amber her training
data has a cutoff and to search rather than guess about anything current — the
direct fix for confidently out-of-date answers.

Persistent memory lives in the `app/memory/` package.

* `store.py` — SQLite, with a real **migration runner** (`amber_schema_version` +
  an ordered `_MIGRATIONS` tuple). A table rather than `PRAGMA user_version`
  because `amber.db` is shared three ways (memory, `agent_mcp`'s usage log,
  `agent_runtime`'s cost rows) and that pragma is a single file-wide slot. The
  store also sets `journal_mode=WAL` and a busy timeout, which is the co-tenancy
  contract `agent_mcp` documents and this store used to be the one tenant ignoring.
* **Facts are tiered.** Every fact carries `tier` (`session`/`short`/`durable`),
  `confidence`, `status` (`active`/`superseded`/`forgotten`), `use_count`,
  `last_used_at`, `superseded_by` and `source`. That's what lets memory curate
  itself: repeated usefulness promotes, disuse decays, a correction supersedes
  rather than accumulating a contradicting twin. `add_fact` **reinforces** an
  existing fact instead of dropping a duplicate — repeat mention is signal. Facts
  are searched through FTS5 (capability-detected at open; `LIKE` fallback for a
  SQLite build without it). Deletion is soft throughout, so a bad correction is
  recoverable.
* `context.py` — `build_memory_view` returns a `MemoryView` (`block`, `items`,
  `facts`, `fact_ids`) from one bounded read: an index search for this utterance,
  unioned with the most-used durable facts *regardless of relevance* (identity-level
  knowledge rarely shares words with the question, so pure relevance ranking drops
  exactly what matters most). Scoring blends relevance, tier, confidence, use count
  and recency; the block is capped by count **and** characters. It used to read every
  fact and tokenize all of them in Python on the latency path.
* `writer.py` — `remember` distils facts after the turn is spoken. Two fixes worth
  knowing: the "already known" list is now **searched, not sampled** (it was the 12
  newest, so the extractor went blind as the store grew), and the exchange is
  **logged before extraction**, so a failure or a barge-in no longer loses the raw
  record too.

The read half runs inline before the brain — injecting the block *and* emitting the
additive `memory` frame (now optionally carrying `{id, content, tier}` alongside the
unchanged `items`). The write half runs after `turn_complete`, and touches the facts
the turn used *before* the model call, so a barge-in loses only the extraction.
Memory is *persistent cross-session knowledge*, distinct from the in-memory
per-connection history in `app/session.py` — don't conflate them.

Tools live in the `app/tools/` package, gated by `AMBER_FEATURE_TOOLS`.
`registry.py` is the pattern: `@registry.register(name, description, input_schema,
available=..., read_only=...)` decorates a Python function (sync or async) returning
a result string; `schemas()` exports the tool list and `dispatch()` runs a call,
converting any error into a string so a bad tool never crashes a turn. Results and
error strings are **clamped** there, so one runaway page of output or an httpx
traceback repr can't poison the rest of the exchange. `dispatch` is also where every
tool call is timed and recorded (see signals below) — one choke point, so nothing
has to instrument itself. Query tools carry `read_only=True`, surfaced as
`x_agent.read_only`, the ecosystem convention.

Inline tools: `search.py` (`web_search`), `fetch.py` (`read_url`), `tasks.py`,
`reminders.py` (`set_reminder`), `memory_tools.py` (`search_memory`,
`remember_fact`, `correct_fact`, `forget_fact`, `list_reminders`,
`complete_reminder` — gated by `feature_memory`), `recall.py` (`recall_recent`, now
searchable) and `update.py` (`update_server`, only when `AMBER_UPDATE_COMMAND` is
set). Heavier or delegated work goes to a **peer MCP server** listed in
`AMBER_MCP_PEERS`.

**Amber curates her own memory.** The memory tools exist because memory used to
happen *to* her: she could not search, commit, correct or forget a fact, so "no, I
moved to Denver" left the wrong fact in every future prompt — and a peer agent over
MCP could search her facts when she couldn't. The split with the automatic writer is
by *intent*: the writer captures what was merely **said** (provisional, `short`,
earning permanence by proving useful); the tools capture what was **meant** — an
explicit "remember this", or a correction — landing `durable` with high confidence
and `source='explicit'`. The tool descriptions lean hard on that, because the failure
mode is Amber calling `remember_fact` every turn and duplicating the writer.

Web search ([app/tools/search.py](app/tools/search.py)) resolves
`AMBER_SEARCH_PROVIDER`: `auto` (**default**) picks `tavily` when
`AMBER_SEARCH_API_KEY` is set and `duckduckgo` when it isn't; an explicit `tavily`
without a key is a config error rather than a silent downgrade. DuckDuckGo falls
back to scraping the HTML results page, because Instant Answers alone answers almost
nothing — best-effort, and it will break when their markup changes. Results are
formatted answer-first with **sourced snippets and their URLs**, which is what makes
`read_url` usable as a second hop. There *was* a third provider — `anthropic`, a
**native server-side** tool the model ran inside its own request, which handled live
queries far better. It went with the brain swap: a server tool only exists inside a
provider's own request loop. **Tavily is the closest replacement, so setting
`AMBER_SEARCH_API_KEY` is the single biggest quality win available.**

`read_url` ([app/tools/fetch.py](app/tools/fetch.py)) fetches one page and hands back
its readable text, extracted by a stdlib `html.parser` subclass
([app/tools/htmltext.py](app/tools/htmltext.py)) — no new dependency; `trafilatura`
is the upgrade if quality demands it. **It is the one genuinely new attack surface
in Amber**: she runs on a VPS beside other services, so `_check_url` refuses
non-HTTP schemes and any host resolving into a loopback/private/link-local/reserved
range, *before* an HTTP client is constructed. The residual DNS-rebinding gap is
documented rather than papered over.

**The agentic loop is no longer Amber's code.** `agent_runtime.AgentRunner` owns
stream → tool call → execute → feed back → repeat; `app/brain.py` composes the
brokers and streams from it. `AnthropicRegistryBroker` adapts the registry above
(`get_tool_schemas()` / `run_tool()`) so a tool stays a plain Python call. Broker
order is priority order — Amber's own tools first, then client tools, then the
`expect_reply` signal, then peers — so a colliding name from a device or a peer can
never shadow hers. The runner works on a copy of the history, so only spoken text is
recorded. It also emits a newline at each tool boundary (`flush_on_tool_call`) so
"let me check that" reaches TTS before the tool runs instead of after — that flush
lives in the shared library now, where every voice agent gets it.

Turn-based conversations (gated by `AMBER_FEATURE_TURN_BASED`) let Amber hold a turn
open when it genuinely expects an answer — *only when necessary*, never every turn.
The brain offers an `expect_reply` tool (`app/brain.py`, `EXPECT_REPLY_TOOL`) as its
own single-tool broker: calling it does no work, it just flips `awaiting_response`
on the per-turn `TurnSignals` back-channel (`app/turn_signals.py`) the pipeline
threads into `think(..., signals=)`, mirroring how `client_tools` is threaded.
Modelling it as a broker rather than an intercepted dispatch keeps the special case
structural instead of buried in a conditional. The brain still yields only text. The
pipeline reads the flag after the stream and sets it on the `turn_complete` frame
(additive optional `awaiting_response`, key present only when true —
`app/protocol.py`); the client keeps the mic open and the next utterance continues
the conversation. Continuation needs no server state — the per-connection history
already chains turns — so nothing is stored on the `Session`. Persona guidance
(`app/persona.py`) tells Amber when to call it.

Client-declared tools (`app/client_tools.py`, gated by `AMBER_FEATURE_CLIENT_TOOLS`)
are per-connection capabilities a *device* can run itself (show text, play a sound).
They live outside the process-wide registry because they are bound to one socket;
the brain offers them prefixed `client_` and dispatches calls back over the WS. They
reach the model through the same `AnthropicRegistryBroker` adapter, because
`ClientTools` already exposes the schema/dispatch pair it wants.

**Getting better unattended.** Two pieces, both off the latency path.

`app/signals.py` records what actually happens: tool outcomes with latency (from
`registry.dispatch`), barge-ins (`main.cancel_current`), user corrections (a
deliberately conservative regex — a false positive costs one junk row, and
over-matching would teach the pass that Amber is wrong constantly), and per-turn
shape. Writes go through a **bounded queue drained by one background task**, and a
full queue drops the oldest row rather than applying backpressure: losing telemetry
is always better than delaying speech. Gated by `AMBER_FEATURE_SIGNALS`.

`app/maintenance.py` runs periodically (started in the lifespan, or by hand with
`python -m app.maintenance`) and curates memory in five steps — **deterministic
first, so a model outage never costs the cheap wins**: decay stale unused low-tier
facts (durable facts never decay), promote short-tier facts that keep proving
useful, consolidate duplicates and contradictions via one bounded LLM call, prune the
exchange log and old telemetry, then write 1–3 short self-review notes from the
telemetry. Every step is individually failure-isolated, idempotent, and bounded —
facts per pass, changes per pass, reflections per pass. Consolidation can only touch
ids it was actually shown, and never drops an `explicit` fact in favour of an
extracted one.

Where the improvement comes from: facts stop duplicating and contradicting, noise
ages out, and useful facts get promoted and start winning retrieval — a compounding
gain in every prompt, with **no prompt mutation at all**. Reflections are *readable*
by default (`amber://memory/reflections`), not injected. Injecting them is
`AMBER_FEATURE_SELF_NOTES`, **off by default** — that flag is the deliberate line
between "Amber notices patterns" and "Amber edits her own instructions". The persona
in git is never rewritten by the model either way.

The agentic loop — stream → tool call → execute → feed results back → repeat — is no
longer Amber's code. `agent_runtime.AgentRunner` owns it, reached through
`AnthropicRegistryBroker`, which adapts this exact registry (`get_tool_schemas()` /
`run_tool()`) so a tool stays a plain Python call. It works on a copy of the history,
so only spoken text is recorded.

[app/mcp_server.py](app/mcp_server.py) is the other direction: Amber's own MCP server
on `agent-mcp-py`, mounted at `/mcp` with `/agent/usage` beside it, exposing
`amber://memory/facts`, `amber://tasks/open`, `amber://reminders/pending`,
`amber://memory/conversations` and `amber://memory/reflections` as resources, plus
`search_memory` / `list_tasks` / `add_task` / `complete_task` as tools — dispatched
through the same registry, never a parallel implementation. (`search_memory` was the
last parallel implementation: a private substring scan that could drift from what
Amber herself sees. It now dispatches like the rest.)

Reliability is concentrated in the transport layer. [app/session.py](app/session.py)
holds, besides `Conversation` (the per-turn history, capped by `max_history_turns`), a
`Session` (stable id + retained conversation + per-session limiter) and a
`SessionManager` (`get_session_manager()`): it mints a session id, keeps a dropped
connection's history warm for `session_ttl_s` so a reconnect with `?session_id=`
resumes, and evicts by TTL / `max_sessions`. [app/ratelimit.py](app/ratelimit.py) is a
sliding-window `RateLimiter` (one per session). [app/main.py](app/main.py) ties it
together: auth via `AMBER_AUTH_SECRET` (`?token=` or `Authorization: Bearer`), the
session handshake (id returned in `ready`), and `_admit_utterance` — the cost
guardrails (`max_audio_bytes`, `rate_limit_turns`/`window`, `max_turns_per_session`)
that reject an utterance with a coded `error` frame *before* any STT/LLM/TTS spend. A
failed turn becomes an `error` frame, never a dropped socket; logs are tagged with the
session id.

## Amber's refactor — done

All three changes landed, and the voice loop and WS protocol are untouched:

1. **OpenClaw removed.** `app/tools/openclaw.py`, its config keys, and its test are
   gone. The inline-vs-delegated distinction survives — delegated work now goes to a
   peer MCP server listed in `AMBER_MCP_PEERS`, merged alongside the inline tools by
   `app.brain.build_broker`. Only the far end changed.
2. **The brain runs on `agent-runtime`.** `app/brain.py` keeps its
   `AsyncIterator[str]` contract and is now a thin wrapper: it builds a broker from
   the existing `app.tools` registry (`AnthropicRegistryBroker` — the same functions,
   so Amber never makes an HTTP call to herself to add a task) and streams from
   `AgentRunner`. `anthropic` is out of `pyproject.toml`; `llm_model` became
   `llm_tier`. `app/memory/writer.py` moved off the Anthropic SDK too — it was the
   other importer, and dropping the dependency would have broken it.
3. **Amber has her own MCP server.** `app/mcp_server.py`, mounted at `/mcp` with
   `/agent/usage` beside it, exposing memory facts, tasks, reminders and
   conversations. Queries are `read_only=True`; its tools dispatch through the same
   registry the brain uses, so there is no parallel code path to drift.

### Things worth knowing about the result

* **One config prefix.** Both libraries can read `AGENT_RUNTIME_*` / `AGENT_MCP_*`,
  and Amber uses neither: `app.brain.runtime_settings` and
  `app.mcp_server.mcp_settings` build their settings objects from `app/config.py`
  with `_env_file=None`. Three consumers, one `AMBER_` prefix, and no chance of two
  sources disagreeing about — most damagingly — which database to write.
* **The MCP server needs keys to mount at all.** `agent-mcp-py` fails closed and
  refuses to build an unauthenticated app, so `mcp_server_enabled` requires
  `AMBER_MCP_KEYS` as well as the feature flag. Otherwise a default install would
  crash at startup instead of quietly not exposing a server nobody asked for.
* **`conversation_id` is the session id**, threaded from `main.py` through
  `run_turn` into the brain, so model spend and tool calls are attributable to a
  session and any peer call joins the same exchange.
* **`X-Confirmed` still has no source.** `app/protocol.py` has 7 server frame types
  and no tool-event frame, so a human cannot approve anything yet. Amber's MCP tools
  are therefore *not* marked `requires_confirmation`: the two mutating ones are
  trivially reversible, and marking them would make them permanently uncallable
  rather than safely gated. Revisit when that additive frame lands.
* **`app/sentence_splitter.py` is now duplicated** by `agent_runtime.streaming`.
  Verified behaviourally identical across abbreviations, decimals and ellipsis, so
  Amber's copy could delegate — left alone deliberately, to keep this refactor's
  blast radius at the three items above.

## Current state (update this section as things change)

- **Amber**: all 5 phases, the ecosystem refactor above, and the memory/prompt
  overhaul (tiered self-curating memory with migrations and FTS retrieval, a
  composed modality-aware prompt, memory-management tools, `auto` search +
  `read_url`, telemetry, and the unattended maintenance pass). Voice pipeline
  complete, streaming seam intact, 353 tests in `tests/`. Runs on `agent-runtime`
  and serves her own MCP server.
  - **Not yet done here:** reminders still can't *fire* — they're recorded,
    listable and completable, but delivery needs a server-initiated protocol frame.
    The maintenance scheduler makes that a small follow-on. Long-term memory as
    markdown files is a future phase; the fact tiering is its on-ramp
    (`durable` + `category` + provenance export cleanly to one file per fact).
- **agent-mcp-py**: **built.** The convention layer — auth, depth guard, usage log,
  sync registration. 180 tests, verified end to end against a live server.
- **agent-runtime**: **built.** The shared agentic loop on OpenRouter's
  OpenAI-compatible endpoint. 110 tests, including a cross-repo interop suite that
  drives a real `agent-mcp-py` server.
- Both are pinned in Amber by **commit SHA**, not tag — neither repo is tagged yet.
  Switch the pins in `pyproject.toml` to `@v0.1.0` once they are.
- **agent-spawner**: not yet built.
- **notification-relay**: not yet built.
- **Hosted sync store**: not yet built, so peer discovery is the static
  `AMBER_MCP_PEERS` map. That is the designed fallback, not a workaround.
- **amber-infra / amber-template**: specced, not yet built.
- **All individual app agents (finance, school, etc.)**: not yet started.
  Finance-agent is the intended first proof point, and is now unblocked — it can be
  built directly on `agent-mcp-py`.
- **FreeCallMe MCP sidecar**: not started — intended as the proof point for the
  "compose external services, don't rewrite the app" pattern.
- **Aperture**: not started, deliberately last — needs enough real agent value to be
  worth unifying before building the shell.

## Build order (current)

1. ~~`agent-mcp-py`~~ — done
2. ~~`agent-runtime`~~ — done
3. ~~Amber refactor~~ — done (OpenClaw out, brain on `agent-runtime`, own MCP server)
4. Hosted config sync store (small; can live inside `notification-relay` or
   standalone). Until it exists, peers are the static `AMBER_MCP_PEERS` map, and
   `sync_client`'s assumed shape (`POST /servers`, `GET /servers`) needs checking
   against the real thing.
5. `agent-spawner`
6. `finance-agent` as first full proof point
7. FreeCallMe MCP sidecar
8. Remaining app agents, shared design system, Aperture — roughly in that order, but
   not strictly blocking each other
