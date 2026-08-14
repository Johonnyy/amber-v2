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
- **Send:** raw audio.
- **Receive:** streamed audio + optional metadata (transcript, thinking state, tool events).
- **Interrupt:** client sends an interrupt message → Amber stops speaking mid-response.

Treat this protocol as a stable public contract. Changing message shapes breaks every
client; additive changes only.

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
(FastAPI + `/ws` + `/mcp` + `/agent/usage`). The "brain" is
[app/brain.py](app/brain.py) — a thin wrapper over `agent_runtime.AgentRunner` —
with its personality in [app/persona.py](app/persona.py) (`compose_system_prompt`
layers the per-turn context blocks onto the persona: the runtime context first,
then the memory block); [app/session.py](app/session.py) holds per-connection
conversation history. [app/responder.py](app/responder.py) is the canned fallback
used when `AMBER_FEATURE_LLM=false`. Both speak the same `AsyncIterator[str]`
contract, so the pipeline downstream of the brain is unchanged.

Per-turn context. Every system prompt is the static persona plus two fresh blocks
the pipeline builds each turn, so the brain always knows "when it is" and "what it
knows": `app/runtime_context.py` (`build_runtime_context`) is a one-line date/time
stamp read in `AMBER_TIMEZONE` (unknown zone → UTC) — *always* injected, independent
of the memory flag; and the memory block below. The conversation history downstream
supplies "what was just said".

Persistent memory (Phase 3) lives in the `app/memory/` package: `store.py` (SQLite
`facts`/`conversations`/`tasks`/`reminders` tables + sync CRUD, `get_store()`),
`writer.py` (`remember` — distil facts from an exchange via a cheap LLM call through
`agent_runtime`, after the turn is spoken), and `context.py` (`build_memory_view` —
rank relevant facts in one store pass into both a compressed prompt *block* for the
system prompt and a flat list of *items* for client display; `build_context` is the
block-only wrapper). On a *cold* turn (the first of a fresh/reconnected session,
empty live history) the pipeline passes `include_recap=True` so `build_memory_view`
appends a short "where you left off" replay of the last
`AMBER_RECENT_RECAP_MESSAGES` durable messages — cross-session continuity the live
history can't give yet; warm turns skip it (history already carries recent context)
and the recap is prompt-only (never in `items`). Gated by `AMBER_FEATURE_MEMORY`;
the read half runs inline before the brain — injecting the block into the prompt
*and* emitting an additive `memory` protocol frame (the same facts, advisory, for
the client's memory panel) — and the write half runs off the latency path after
`turn_complete`. Memory is *persistent cross-session knowledge*, distinct from the
in-memory per-connection history in `app/session.py` — don't conflate them.

Tools (Phase 4) live in the `app/tools/` package, gated by `AMBER_FEATURE_TOOLS`.
`registry.py` is the pattern: `@registry.register(name, description, input_schema)`
decorates a Python function (sync or async) returning a result string; `schemas()`
exports the tool list and `dispatch()` runs a call, converting any error into a
string so a bad tool never crashes a turn. A tool may carry an `available()`
predicate — unavailable tools are hidden and refuse to run. Inline tools:
`search.py`, `tasks.py` (`add_task`/`list_tasks`/`complete_task` over the store),
`reminders.py` (`set_reminder` — persists to the `reminders` table; firing/delivery
is future work), `recall.py` (`recall_recent`, gated by `feature_memory`) and
`update.py` (`update_server`, only offered when `AMBER_UPDATE_COMMAND` is set).
Heavier or delegated work goes to a **peer MCP server** listed in
`AMBER_MCP_PEERS`; the OpenClaw HTTP bridge that used to fill that role is gone.

Web search is self-dispatched only, by `AMBER_SEARCH_PROVIDER`
([app/tools/search.py](app/tools/search.py)): `duckduckgo` (keyless default, canned
Instant Answers, misses most current events) or `tavily` (keyed, much better). There
*was* a third — `anthropic`, a **native server-side** tool the model ran inside its
own request, which was the default and handled live queries far better. It went with
the brain swap: a server tool only exists inside a provider's own request loop, and
the OpenAI-compatible endpoint has no equivalent. `pause_turn` handling went with
it, since it existed only to resume a server tool. **If current-events quality
matters, set `AMBER_SEARCH_API_KEY` and select `tavily`.**

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

[app/mcp_server.py](app/mcp_server.py) is the other direction: Amber's own MCP
server on `agent-mcp-py`, mounted at `/mcp` with `/agent/usage` beside it, exposing
`amber://memory/facts`, `amber://tasks/open`, `amber://reminders/pending` and
`amber://memory/conversations` as resources, plus `search_memory` / `list_tasks` /
`add_task` / `complete_task` as tools — dispatched through the same registry, never
a parallel implementation.

The agentic loop — stream → tool call → execute → feed results back → repeat — is no
longer Amber's code. `agent_runtime.AgentRunner` owns it, reached through
`AnthropicRegistryBroker`, which adapts this exact registry (`get_tool_schemas()` /
`run_tool()`) so a tool stays a plain Python call. It works on a copy of the history,
so only spoken text is recorded.

[app/mcp_server.py](app/mcp_server.py) is the other direction: Amber's own MCP server
on `agent-mcp-py`, exposing `amber://memory/facts`, `amber://tasks/open`,
`amber://reminders/pending` and `amber://memory/conversations` as resources, plus
`search_memory` / `list_tasks` / `add_task` / `complete_task` as tools — dispatched
through the same registry, never a parallel implementation.

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

- **Amber**: all 5 phases plus the ecosystem refactor above. Voice pipeline complete,
  streaming seam intact, 133 tests in `tests/`. Runs on `agent-runtime` and serves
  her own MCP server.
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
