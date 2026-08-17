# CLAUDE.md — The Amber Ecosystem

This file describes the whole project: what it is, how the pieces fit together, and
the conventions every repo in the ecosystem follows. Copy the relevant sections into
a repo-specific CLAUDE.md when working inside an individual app; this version is the
canonical, ecosystem-wide reference.

It also documents **this** repo (`amber`) in detail — see [This repo:
Amber](#this-repo-amber) near the bottom. Amber, the two shared libraries, the sync
store and (in progress) Aperture exist; the individual domain apps do not yet. Keep
"designed" and "built" clearly separated when reading — the Current state section is
the authority on which is which.

## What this is

A personal, open-source ecosystem of independent apps (finance, school, project
tracking, FreeCallMe's dashboard, etc.), each usable completely standalone, that also
expose themselves to a personal AI layer via MCP. **Amber** is the orchestrating
voice/text agent that knows Johnny and can query or act across every connected app.
**Aperture** is the unifying Electron shell that ties the apps together visually and
manages device config/sync — and since new apps ship backend-only, it is *the* place
their data is seen and operated. Every app can be cloned and run alone by anyone — the
agent layer is always an opt-in extension, never a dependency.

## Core principle (do not violate)

**Every app must run standalone with zero knowledge the ecosystem exists.** If `git
clone`-ing a single app and running it requires anything from another repo, that's a
bug in the design. The agent/MCP layer is always optional, toggled by a feature flag,
off by default.

## Design principles (treat these as binding)

These four decide *how* a feature is built, the way the principle above decides *what
it may depend on*. They apply to every new app and every new Aperture feature.

**1. Aperture is visual first.** Reach for a visual representation before text. If the
data has a shape — time, quantity, hierarchy, geography, state, relationship — render
that shape: a chart, a timeline, a map, a graph, a status board, a diff view. A table
beats a paragraph; a chart beats a table when the point is a trend. Text is what you
fall back to when nothing visual would carry the information, not the default a visual
has to argue its way past. The reason a shell over a pile of backends is worth building
at all is that a screen shows at a glance what a transcript makes you read.

**2. If it can't be done in the UI, it isn't done.** Every configuration, fix, and
recovery path must exist in Aperture. Editing a `.env` on the box, running a migration
by hand, `systemctl restart`, poking SQLite — each of those needs a UI equivalent
before the feature counts as shipped. Dropping to a terminal to make something work is
a bug in Aperture, not a documentation gap; the whole purpose is defeated the moment
SSH becomes load-bearing.

**3. Amber is the brain — everything is reachable in natural language.** Aperture does
not grow a second intelligence layer. Anything the UI can do, Amber can do when asked,
driving the *same* underlying setting or action the UI writes — never a parallel path.
Adding a feature means adding the tool (or protocol frame) that lets Amber drive it, in
the same change, not as a follow-on. The canonical case: **"can you talk slower?" lowers
the TTS speed in settings**, and the Settings page shows the new value. A feature only a
click can reach is half-built; so is one only Amber can reach.

**4. New apps are backend-only for now.** A new app ships its API and its MCP server;
its own dashboard is **deferred**. Aperture is where its data becomes visible and Amber
is where it becomes conversational, so a per-app frontend is duplicated work until the
app earns one. This does not weaken the standalone principle — the app still runs alone
and is still fully usable through its own API. Next.js remains the default *when* a
frontend is finally warranted.

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
| **Aperture** | Electron shell app — the visual-first UI for the whole ecosystem, device-local config store, import/export/sync |
| **agent-spawner** / **Bloom** | Service wrapping `agent-runtime` — where an agent is *defined* (prompt + model keyword + connections) rather than built, and where delegated tasks run. Built, as the `bloom` repo; "agent-spawner" is the design-time name these docs still use |
| **notification-relay** | Push notification fan-out (Redis pub/sub → APNs → iOS) |
| **finance-agent, school-agent, outpost, freecallme, etc.** | Individual domain apps — backend + MCP server; their own frontend is deferred, Aperture renders them |

Naming style for future apps: single clean nouns, consistent with
Outpost/ThinkTank/Aperture (Forge, Sentinel, Herald, Atlas, etc. — see naming
brainstorm history for the full candidate list, check for collisions before
assigning).

## Repo list

- `amber` — the agent itself (**this repo**; built, working, and refactored onto the
  shared libraries — see below)
- `agent-mcp-py` — shared library: wraps the MCP Python SDK with auth, depth-guard,
  usage logging, sync-store registration. Every Python app's MCP server is built on this.
- `agent-runtime` — shared library: the actual agentic loop (call model → tool call →
  execute → repeat), built on OpenRouter's OpenAI-compatible endpoint. Imported
  directly (not called over network) by Amber and agent-spawner.
- `bloom` (the repo the docs call `agent-spawner`) — service, imports `agent-runtime`
  in-process, exposes task delegation and agent creation as MCP tools plus a REST
  admin API for Aperture.
- `notification-relay` — service, Redis pub/sub, single `send_notification` endpoint
  (also exposed as an MCP tool).
- `amber-infra` — deployment backbone: Caddy config, install script, backup scripts,
  CI templates, the hosted config sync store.
- `amber-template` — scaffold repo, pre-wired with `agent-mcp-py`, Docker, CI,
  backups. `npx degit` starting point for every new app.
- `Aperture` — Electron shell; the visual surface for everything, and the only place
  the ecosystem is meant to be operated from.
- Individual app repos (`finance-agent`, `outpost`, etc.) — each standalone, each
  optionally MCP-enabled, each **backend-only for now** (see design principle 4).
- `freecallme` — existing Next.js/Vercel/Supabase app, **not rewritten**; gets a small
  TypeScript MCP sidecar added.

## Tech stack decisions

- **New backend-heavy apps default to Python (FastAPI)** — this is what lets them
  share `agent-mcp-py` and `agent-runtime` with Amber and the spawner.
- **New apps ship no frontend.** Per-app dashboards are deferred (design principle 4);
  build the API + MCP server and let Aperture render it. When an app does eventually
  earn its own frontend, the default is still Next.js (React).
- **Existing apps are not rewritten to match the pattern.** FreeCallMe stays Next.js;
  it gets an MCP server written in TypeScript (`@modelcontextprotocol/sdk`) as a
  sidecar, not a port to Python. MCP is the interop layer specifically so language
  doesn't have to match everywhere — only the protocol does. Apps that already *have*
  a frontend keep it; the deferral is about not building new ones.
- **Aperture** is Electron + React, frontend-only, not itself an MCP server. It is a
  **client** of every app's MCP server and of Amber's WS protocol — which is exactly
  why per-app dashboards can be skipped: an app that exposes resources and tools is
  already renderable, and already conversational.
- **Visualisation is a first-class dependency in Aperture**, not an afterthought
  bolted on when a screen looks bare. Picking the chart/graph/board library and the
  shared design tokens is part of building the shell, because principle 1 makes them
  load-bearing.
- **Registry / service discovery** is not a static YAML file — it's a small hosted sync
  store (living alongside `notification-relay` or similar always-on service) that
  Aperture edits through a UI and every headless agent (Amber, spawner, apps) reads
  directly. Aperture's on-device storage is a cache + what enables import/export and
  multi-device sync; it is never the only copy, since headless services must work even
  when Aperture isn't open.

## Conventions every app must follow

- **Resource URIs mirror real views.** Every view Aperture renders has a matching MCP
  resource returning the same data (e.g. `finance://transactions/recent`). With per-app
  frontends deferred this reads in the other direction too: the resources an app exposes
  *are* the views, and Aperture builds its screens from them. No separate "agent-only"
  version of the data.
- **Tools mirror real user actions.** If a human can click it, there's a tool that does
  the same thing, calling the same underlying function as the UI — not a parallel code
  path that can drift.
- **Every feature ships UI-reachable and Amber-reachable, in the same change.** The two
  are not separate milestones: a setting the Settings page can write must have a tool or
  frame Amber can drive, both landing on the same underlying value. This is design
  principles 2 and 3 stated as an acceptance criterion — a PR that adds a capability
  reachable only one way isn't finished.
- **Anything an operator would SSH for gets a UI path.** Restarts, config edits,
  migrations, log reads, key rotation. If the runbook says "ssh in and…", that step is
  a missing Aperture feature.
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
  Aperture (now under way — see Current state).
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
performance-critical seam; keep it intact when modifying the pipeline. The
`agent-runtime` swap (done) changed only what produces the token stream, never this
seam — and nothing since should either.

### Client protocol (WebSocket)

Every client speaks the same protocol — building a new client means writing a thin
wrapper around it:
- **Send:** raw audio, or a `user_text` frame when the client already has the words.
- **Receive:** streamed audio + optional metadata (transcript, thinking state, tool events).
- **Interrupt:** client sends an interrupt message → Amber stops speaking mid-response.

Treat this protocol as a stable public contract. Changing message shapes breaks every
client; additive changes only.

**Voice settings are per-connection.** TTS used to read `settings.tts_*` at synthesis
time, which made the voice a process-wide constant — changing it meant editing `.env`
on the box and restarting, and every client got the same one. `app/voice.py` makes it
a value (`VoiceSettings`: model, voice, container, speed, instructions) that lives on
the `Session`, defaults from config, and is patched over the wire by `set_voice`. The
server answers with a `voice` frame carrying the *effective* settings — validated,
clamped, and reset-capable — plus the catalogue of what this Amber accepts, so a
client builds its picker from the wire rather than shipping a list that drifts. An
explicit `null` in a patch means "back to the server's default", which is what makes
"use Amber's own setting" a state a UI can return to rather than only leave. The
frame is read once per turn in `run_turn`, so a change mid-reply lands on the next
turn instead of switching voices between two sentences. Gated by
`AMBER_FEATURE_VOICE_CONTROL` (on); off pins every connection to the config and marks
the `voice` frame `locked` so a client shows the values read-only instead of letting
them silently snap back.

The one real subtlety is that OpenAI's two TTS generations take opposite parameters:
`tts-1`/`tts-1-hd` accept `speed` and reject `instructions`, while `gpt-4o-mini-tts`
is the reverse and ignores `speed` entirely. `speech_params` resolves that in one
place — on a model that ignores speed, a non-default speed becomes a pacing sentence
prepended to the instructions, so the knob means one thing on both rather than
silently doing nothing on the better-sounding model.

**Model choice is per-connection too, and by keyword.** You pick a brain by
*describing* it — `fast`, `cheap`, `balanced`, `strong`, `coding`, `reasoning`,
`writing`, `research`, `vision`, `long` — and `app/models.py` resolves the word to an
OpenRouter id at the last possible moment, inside `think`. Three layers, in order: a
literal id (anything with a `/`) passes through; then this install's overrides, rows
in SQLite; then the built-in defaults. Resolution **never raises** on the turn path —
an unknown keyword falls back to the install default and logs, because a typo in
`.env` must cost one reply from the wrong model, not every reply.

Two things move at runtime, and they are different scopes. `set_model`'s `keyword`
picks the brain for *this connection* (`null` = back to `AMBER_LLM_TIER`), lives on
the `Session` beside the voice, and is read once per turn. Its `map`
(`{"coding": "vendor/model"}`, `null` to reset) re-points what a keyword *means* for
the whole install, is persisted, and outlives the socket — the map is applied first,
so one frame can invent a keyword and select it. The `model` frame answers with the
effective values plus the whole catalogue (each keyword's description, what it points
at, what it would point at untouched), so a picker is built from the wire. Gated by
`AMBER_FEATURE_MODEL_CONTROL`; off marks the frame `locked`. `memory_tier` and
`maintenance_tier` are keywords through the same table, resolved where their runners
are built.

**The keyword table is shared with the ecosystem.** `app/model_sync.py` pushes this
install's overrides to the sync store's `/models` and pulls everyone else's, so
`coding` means one model in every app rather than one per repo. Local always wins
until it is pushed: a change is written to SQLite and takes effect on the next turn
whether or not the store is reachable, and reconciliation is a background pass (on
startup, on a timer, and immediately after a client edit). A reset while the store is
down leaves a **tombstone** — a row with an empty model — because otherwise the next
pull would silently restore the override. With no `AMBER_MCP_SYNC_STORE_URL` the
whole module is inert and the table is simply Amber's. Conflicts are last-write-wins,
which is the honest fit for a one-person ecosystem.

**A turn is now visible while it happens.** Two additive server→client frames close
the gap where a client had two states to render — thinking and speaking — for a turn
that might have run a search, corrected a memory and handed a two-minute build to
Bloom in between. `activity` reports every tool call twice, on dispatch and on
return, correlated by id and tagged with which of the four brokers served it
(`own` / `client` / `signal` / `peer:<name>`, recovered from the naming convention by
`peers.classify_tool_name`). `delta` carries the raw reply text before the sentence
splitter sees it — `audio_chunk` is the *speech* view and loses the model's own
whitespace, so a client that concatenates sentences can never render a heading, a
list or a code fence.

The seam is `app/activity.py`: a `ToolBroker` decorator wrapping the assembled
broker in `build_broker`. Not `registry.dispatch`, which looks like the obvious
choke point and is only Amber's *own* tools — instrumenting there would have shown
the searches and hidden every `bloom__*` call, which are the slow ones worth
watching. Wrapping the composite covers all four classes and closes the matching
telemetry hole on the way past. Two rules are load-bearing: emission never raises,
and a barge-in emits **nothing** — that path also runs when the connection is
closing, so the socket may be gone, and it sits between the user interrupting and
Amber listening again. A call still open at `turn_complete` is an interrupted call,
and the client already knows because it sent the `interrupt`.

**Amber can speak first now, and that was one gap wearing four masks.** Every frame
above is a *reply* — the whole protocol was request/response per utterance, and there
was no way for the server to originate anything. That single structural fact was the
shared root of four separately-half-built features: reminders that could be recorded,
listed and completed but never *fire*; `requires_confirmation` unusable ecosystem-wide
because `X-Confirmed` had no source; maintenance reflections written every cycle and
readable only by an agent holding an MCP key; and a Bloom build finishing on another
tab in silence.

`push` ([app/push.py](app/push.py)) is that direction. Three things separate it from
every advisory frame above. It is **durable** — a SQLite outbox, not a queue, because
a reminder due while nothing is connected is precisely the case that must survive, and
Amber restarts on every deploy. Delivery is therefore **at-least-once with a stable
`id`**: the row is marked *after* a successful send, so a crash in between redelivers
rather than loses, and the client dedupes. And it **never lands mid-turn** — that is a
correctness rule, not politeness. The pipeline sends an `audio_chunk` and then the
bytes it describes, with an `await` boundary between them; a background task writing
there would corrupt the audio stream for every client. A lock per send does *not* fix
it (the pusher takes the lock in the gap), so the deliverer waits for the connection
to be idle instead, which removes the hazard rather than narrowing it. In-turn writers
— `activity`, `delta`, `confirm_request` — are safe by construction because they run on
the task already driving the turn.

The sink registry generalises `session.client_tools.bind(send_json)`, which was the
only place a live connection was ever reachable from outside its own handler. Fan-out
is honest about being single-user: a push goes to whoever is listening and is settled
once any sink takes it. `POST /push` (authenticated with `AMBER_MCP_KEYS`) is how
another service triggers one — the frame made delivery trivial, but nothing could
*trigger* a "your build finished", since work completing inside a peer is invisible to
Amber once the turn that started it ends. `reminder` is refused there: those are minted
by the scheduler against a real row, and a forged one would put a reminder in front of
the user with nothing behind it.

**Reminders fire** ([app/reminders.py](app/reminders.py)), and making them fire exposed
a bug that had never had the chance to be wrong out loud. `set_reminder` asks the model
for local time "with no offset" and stored that string verbatim, while every other
timestamp is UTC — so `due_reminders`' lexical comparison mis-fired by the length of
the user's offset, six hours early in Denver. A trailing `Z` was also accepted and kept,
so the column held two conventions at once. Times are normalised to an aware instant on
write now, and the scheduler compares *instants* in Python rather than trusting SQL,
because pre-existing naive rows are still in the database. `fired_at` (migration `_m8`)
keeps delivery distinct from completion: a reminder that was announced and not acted on
is still pending, and collapsing those would mean announcing it ticked it off.

**`confirm_request` / `confirm_response`** ([app/confirm.py](app/confirm.py)) is the
approval source. `agent-mcp-py` always enforced `requires_confirmation` and
`agent-runtime` always published it on every tool schema, local and peer alike; what was
missing was anything that could honestly produce the header, which is why marking a tool
would have made it *permanently uncallable* rather than safely gated. `Confirmations`
lives on the `Session` for the same reason `ClientTools` does — the receive loop has the
session, so an answer can find the call blocked on it — and `ConfirmBroker` wraps the
composite *inside* the activity wrap, so a denied call still renders as a tool call that
failed instead of vanishing. It **fails closed**: no client, no answer, or a denial all
refuse, and the three are reported distinctly because the model should re-ask after
silence and drop it after a no.

One trap is worth remembering: `MCPClient.bind` is **run-scoped** and sets conversation
id, depth and confirmed together, so binding `confirmed=True` alone silently detaches
the call from its conversation and zeroes the depth guard. `ConfirmBroker` records what
it was bound with and re-binds the whole set around the approved call. That is safe only
because the runner executes tool calls sequentially and Amber rebuilds the broker per
turn — both verified, and both worth re-checking if `agent-runtime` ever parallelises
tool execution. `update_server` is the first tool marked, and the obvious one: it runs a
shell command and restarts the process serving the conversation that asked for it.
Amber's *own* MCP tools stay unmarked deliberately — that file gates the **inbound**
direction, whose approval source is the caller's own header, not this frame.

**Principle 3 is closed: Amber drives her own settings now.** "Can you talk slower?"
lowers `speed` rather than producing a sentence about talking slower
([app/knobs.py](app/knobs.py), `_knobs_broker` in [app/brain.py](app/brain.py)). The
mechanics turned out to be free — `session.voice` and `session.model_keyword` are read
exactly once per turn, into locals `run_turn` then holds frozen, so a tool that mutates
the session **cannot** change the voice of sentence four. It lands on the next turn by
construction.

What was *not* free is reachability, and it is worth knowing why the obvious approach is
impossible: the registry is process-wide, `dispatch` passes only the JSON the model
produced, `available=` predicates take no arguments, and there is no contextvar anywhere
in this codebase. A `@registry.register` tool cannot see its connection at all. So the
tools are closures over a per-turn `Knobs` in their own `LocalToolBroker`, exactly the
shape `_signal_broker` uses for `expect_reply`, threaded down the path `confirmations`
already takes. Every method delegates to the code the *client frames* use —
`VoiceSettings.patched`, `models.apply_map` — so asking and clicking are validated by
the same lines and land on the same value.

**The confirmation posture is the design point, not a detail.** `set_voice` and
`set_brain` are per-connection and undone by saying so again. `remap_keyword` is
install-wide, persisted to SQLite and pushed to the sync store, so a passing remark would
change what `coding` means in *every app in the ecosystem* — it alone carries
`requires_confirmation`, which is the approval pair's first real use beyond
`update_server`. And the prompt gains a **settings block** naming the live values
(`_settings_block`, interpolated like `_device_block`), because "slower" is unanswerable
without knowing the current speed.

**Everything above is a reply; three more frames are how you ask how she's *doing*.**
`review_query` / `review` / `review_action` ([app/review.py](app/review.py)) share one
trio shaped exactly like the memory one, covering three topics: per-tool reliability
(p50/p95, nearest-rank, matching what `agent_mcp.usage_log` reports for its own table),
the maintenance pass's self-review notes, and eval cases. All three read data Amber has
always recorded and shown nobody. Reflections gain **promote** and **dismiss** —
`reflections.dismissed` had existed since the table was created with no writer at all —
and promotion is what makes `AMBER_FEATURE_SELF_NOTES` safe to leave off: the note
becomes an ordinary durable fact because *a person chose to keep it*, not because the
model edited its own instructions. `eval_capture` saves a turn that went wrong, carrying
the whole case rather than a pointer into the exchange log, since that table has no
session id and pairs user with assistant positionally.

**A turn now says where its seconds went.** `turn_complete` carries `timings`
([app/timings.py](app/timings.py)) and `step_spans`. Two `perf_counter` pairs are the
whole addition — per-step model times have always been on `RunState.steps` and `_stats`
threw them away. Deliberately **no `tools_ms`**: every tool call already arrives as an
`activity` pair carrying its own `ms`, and measuring it twice would be two sources for
one number. On the way past, `signals.record(KIND_TURN, latency_ms=)` was hard-coded
`None`, so no turn had ever recorded its own duration.

The standing rule that came out of this: **every new setting ships with its
Amber-facing tool in the same change.** The gap above existed for as long as it did
because each control was built client-first and the tool was left as a follow-on that
never came.

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
**keyword** (`llm_tier`) resolved by `app/models.py`, not a literal model id — and
only the *default*, since a connection can pick another and the table itself is
re-pointable at runtime.

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

Key modules: [app/config.py](app/config.py) (all keywords/keys/flags),
[app/models.py](app/models.py) (the model keyword catalogue) and
[app/model_sync.py](app/model_sync.py) (sharing it through the sync store),
[app/protocol.py](app/protocol.py) (WS wire contract),
[app/sentence_splitter.py](app/sentence_splitter.py) (streaming seam),
[app/pipeline.py](app/pipeline.py) (the voice loop), [app/main.py](app/main.py)
(FastAPI + `/ws` + `/mcp` + `/agent/usage`, and the lifespan that starts the signal
writer and the maintenance loop). The "brain" is
[app/brain.py](app/brain.py) — a thin wrapper over `agent_runtime.AgentRunner` —
with its personality in [app/persona.py](app/persona.py) (`compose_system_prompt`
composes the core, the modality block, the ecosystem block
([app/ecosystem.py](app/ecosystem.py)), the device block, and the per-turn context);
[app/signals.py](app/signals.py) and [app/maintenance.py](app/maintenance.py) are
the self-improvement loop; [app/session.py](app/session.py) holds per-connection
conversation history. [app/responder.py](app/responder.py) is the canned fallback
used when `AMBER_FEATURE_LLM=false`. Both speak the same `AsyncIterator[str]`
contract, so the pipeline downstream of the brain is unchanged.

The system prompt is **composed, not monolithic** ([app/persona.py](app/persona.py)).
`compose_system_prompt` is keyword-only and layers a fixed `CORE` (identity, how to
answer, tools, memory posture) with blocks that only apply to this turn: a
**modality** block (`SPOKEN_STYLE` for audio turns, `TYPED_STYLE` for `user_text`
ones — the pipeline derives it from `text is not None`), an **ecosystem** block, a
**device** block naming the client tools this connection actually declared, and the
per-turn context blocks.
That structure exists because one flat prompt drifted badly: it named a deleted
backend three times, banned emoji then required one, described display tools a
headless client never had, and gave "spell it for the ear" rules to typed replies.
A block that's only emitted when it applies can't rot the same way. `SYSTEM_PROMPT`
is `compose_system_prompt()` with no context — the brain's default.

**Amber knows what she's part of** ([app/ecosystem.py](app/ecosystem.py),
`build_ecosystem_block`, gated by `AMBER_FEATURE_ECOSYSTEM_CONTEXT`, on). She is the
natural-language way into this ecosystem, so "what's Bloom?" or "how do you work?"
must not send her to a web search for something private. The block names each piece —
herself, Aperture, `agent-runtime`, `agent-mcp-py`, `amber-infra` and its sync store,
Bloom — in one line each, saying what it's *for*. Two rules keep it from becoming the
bloat `persona.py` was written to end. **Framework knowledge, not documentation:** no
paths, config keys or endpoint names, because none of that can be spoken and all of it
rots when the code moves (a test asserts they're absent). **Claims are checked against
this install, not the plan:** the static half describes the shape, and `_wiring_block`
appends only what this process can really reach — peers by their real names, read
through the same `load_static_peers` the broker uses, the sync store when one is
configured, "other agents can query you" only when the MCP server actually mounts. It
closes by telling her the domain apps are planned rather than built, and that a
missing tool means a missing capability — the failure mode of handing a model an
architecture diagram is that it starts describing planned features in the present
tense. It sits with identity rather than with the per-turn blocks, since it's
background about who she is, and it's budgeted (a test caps it) because every turn
pays for it.

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
set). Heavier or delegated work goes to a **peer MCP server** — from `AMBER_MCP_PEERS`
*and* from whatever has registered with the sync store, unioned by
`agent_mcp.PeerRegistry` ([app/peers.py](app/peers.py)) with the static map winning.

That union is new, and the bug it closes is worth remembering because it had **no
symptom at all**. `build_broker` read only the static map, and passed that same dict
as `MCPClient`'s `resolver` — which returns from its `Mapping` branch before it could
ever consult `agent_mcp.registry`. So an empty `AMBER_MCP_PEERS` built no MCP client
whatsoever, and Bloom could register perfectly, appear in `GET /servers`, mount its
MCP server and answer 401 to an unauthenticated probe while Amber named her own
thirteen tools and no `bloom__*` ones. Nothing logged anything, on either side: an
unlisted peer is not a tool that fails, it is no tool. The store had always held a
credential per server and `PeerRegistry` had always kept a two-layer cache; neither
was reachable from here. `AMBER_FEATURE_PEER_DISCOVERY` (on) is the switch, and off
pins her to the static map — which is what you want when a bad registry entry is
sending her somewhere she should not go, since static wins anyway.

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
* **`X-Confirmed` has a source now** — `confirm_request` / `confirm_response`, see
  [app/confirm.py](app/confirm.py). It gates the **outbound** direction: Amber asking
  before *she* calls a tool that declares `requires_confirmation`. Amber's own MCP
  tools stay unmarked, and that is a distinction rather than an oversight — that file
  is the **inbound** direction, where the approval source is the calling agent's own
  header, not this frame. Marking `add_task` there would pop a dialog on a possibly
  absent human on behalf of a peer, and both mutating tools remain trivially
  reversible.
* **`app/sentence_splitter.py` is now duplicated** by `agent_runtime.streaming`.
  Verified behaviourally identical across abbreviations, decimals and ellipsis, so
  Amber's copy could delegate — left alone deliberately, to keep this refactor's
  blast radius at the three items above.

## Current state (update this section as things change)

- **Amber**: all 5 phases, the ecosystem refactor above, and the memory/prompt
  overhaul (tiered self-curating memory with migrations and FTS retrieval, a
  composed modality-aware prompt, memory-management tools, `auto` search +
  `read_url`, telemetry, and the unattended maintenance pass), plus per-connection
  voice and model control with a keyword table shared through the sync store, and
  ecosystem self-knowledge in the prompt. Voice pipeline complete, streaming seam
  intact, 675 tests in `tests/`. Runs on `agent-runtime` and serves her own MCP
  server, and resolves peers through the registry as well as the env map
  ([app/peers.py](app/peers.py)).
  - **New: Amber can speak first.** The protocol had no server-initiated frame at
    all, and that one absence was blocking four things at once. `push` +
    a durable outbox ([app/push.py](app/push.py), migration `_m7`) is the direction;
    **reminders now fire** ([app/reminders.py](app/reminders.py), `_m8` adds
    `fired_at`), reflections surface instead of sitting unread, and `POST /push` lets
    any ecosystem service put something in front of the user. `confirm_request` /
    `confirm_response` ([app/confirm.py](app/confirm.py)) is the `X-Confirmed` source
    that was missing, so `requires_confirmation` is usable **outbound across the whole
    ecosystem** — which is what lets Bloom gate `run_task`. `update_server` is the
    first tool marked. See the client protocol section for the three rules
    that hold `push` together (durable, at-least-once, never mid-turn).
  - **Fixed on the way past, and it would have been wrong every time:** a reminder's
    `remind_at` was stored exactly as the model wrote it — local time with no offset,
    per the tool's own description — while everything else in the database is UTC. It
    had never mattered because nothing ever compared them. `due_reminders` did, so
    every reminder would have fired early by the user's offset.
  - **Not yet done here:** long-term memory as markdown files is a future phase; the
    fact tiering is its on-ramp (`durable` + `category` + provenance export cleanly to
    one file per fact).
  - **Principle 3 is closed.** [app/knobs.py](app/knobs.py) gives Amber `set_voice`,
    `set_brain` and `remap_keyword`, driving the *same* per-connection values the
    Settings page writes. The asymmetry is the point: only the install-wide,
    persisted, ecosystem-shared `remap_keyword` requires confirmation. The
    reachability problem is worth reading before adding another such tool — the
    process-wide registry genuinely cannot see a connection, so these live in a
    closure-captured `LocalToolBroker`.
  - **New: the data Amber had is finally visible.** A turn reports where its seconds
    went ([app/timings.py](app/timings.py)); `review_query` / `review` /
    `review_action` ([app/review.py](app/review.py)) surface per-tool reliability
    with real percentiles, the self-review notes (now promotable into durable facts,
    or dismissable — `reflections.dismissed` finally has a writer), and eval cases
    captured from turns that misfired and replayed by
    [app/evals.py](app/evals.py). `superseded_by` has been written on every
    correction since tiering landed and read by **nothing**; `memory_query` gains
    `scope: lineage | archive` so a fact's revision history and the archive of what
    she no longer believes are both readable. `status` carries the memory *policy*,
    so a client can say "forgotten in about 4 days unless used" without hardcoding
    thresholds that go quietly wrong on a tuned install. 675 tests.
  - **The turn is observable.** `activity` and `delta` frames
    ([app/protocol.py](app/protocol.py), [app/activity.py](app/activity.py)), a
    `status` frame carrying peers/sync/features ([app/status.py](app/status.py)),
    and client-driven memory curation — `memory_action` / `memory_query`
    ([app/memory_control.py](app/memory_control.py)) landing on the *same* store
    functions the `forget_fact` / `correct_fact` tools call, so a fact forgotten by
    asking and one forgotten by clicking are the same row.
  - **Fixed, and it had never worked:** `AgentRunner.stream` built its bookkeeping
    state inline and discarded it, and the recording step was reachable only from
    `run()` — which Amber never calls. So every voice turn threw away its model,
    token counts, cost and timings, and **Amber had never written a single cost
    row**, despite `runtime_settings` carefully pointing the tracker at her own
    database. `stream(state=)` is now additive upstream and `brain.record_spend`
    writes after the reply is out; `turn_complete` carries the numbers.
- **agent-mcp-py**: **built.** The convention layer — auth, depth guard, usage log,
  sync registration. 180 tests, verified end to end against a live server.
- **agent-runtime**: **built.** The shared agentic loop on OpenRouter's
  OpenAI-compatible endpoint. 110 tests, including a cross-repo interop suite that
  drives a real `agent-mcp-py` server.
- Both are pinned in Amber by **commit SHA**, not tag — neither repo is tagged yet.
  Switch the pins in `pyproject.toml` to `@v0.1.0` once they are.
- **agent-spawner**: **built, and it's called `bloom`.** The name in these docs is
  the design-time one; the repo is `bloom`. It went further than the original spec:
  rather than routing tasks to models, it's where an *agent is defined instead of
  built* — a prompt, a model keyword, and the connections (OAuth accounts, API keys,
  MCP servers) it may act through, from a global library where approving a service
  once makes it available to every agent. It can also build one from a description
  ("a Spotify agent that can play and search music"), researching the service and
  preferring an existing MCP server to an integration it would have to carry. Two
  surfaces with deliberately separate key sets: `/mcp` (`run_task`, `build_agent`,
  `edit_agent`) is how Amber and other agents reach it, `/admin/*` is plain REST
  because Aperture wants an OpenAPI schema, and a GUI that edits config shouldn't
  hold a token that spends money. Adding a capability is a row in a table, not a repo
  and a deploy — which is what makes design principle 4 affordable.
  - **It writes its own provider manifests now.** A provider — the definition of how
    to reach one service's API with a credential — used to be a TOML file in Bloom's
    code tree, which made "connect Google Analytics" a pull request and a redeploy.
    There is no version of *ship a manifest for every OAuth service* that scales, and
    it contradicted the premise that a capability is a row in a table. The builder
    researches the API from its own documentation and writes the manifest at runtime;
    it lives in `bloom.db`, not the repo, and travels through the sync store's
    `/manifests` so one install's research is not repeated on the next. Two files
    remain in `app/providers/` as reference implementations and always beat a stored
    row of the same name. The trade is real and priced rather than waved away: a
    manifest defines HTTP calls made with a live credential, so endpoints must be
    public https (a manifest naming `169.254.169.254` would point the credential
    resolver at the instance's own metadata service), `DELETE` is refused outright,
    and — since the reviewing human was the thing given up — the connection screen now
    says who wrote the definition and **which hosts the credential will be sent to**,
    which is a fact someone can actually check. `PUT /admin/manifests/{name}` is the
    acceptance criterion rather than a nicety: a wrong operation is fixed in a form,
    because fixing it in an editor would have traded one code-editor trip for another.
    `docs/provider-manifests-future.md` in Bloom is the full accounting.
  - **It edits as well as builds** (`edit_agent`, `POST /admin/builder/edit`, and the
    matching builder tools). It could only create, which made "give the Spotify agent
    permission to skip" unanswerable — the only offer available was a rebuild, and a
    rebuilt agent attached to the same connection inherits the same OAuth grant. **A
    permission is a property of the connection, not of the agent**; both the tool
    descriptions and the builder's prompt name the rebuild as the wrong move, because
    it is what a model reaches for unprompted. Widening a scope still cannot be
    finished headlessly — a provider grants scopes when a person approves them — so
    an edit hands back an authorisation link and a `connect_oauth` step, and settles
    `needs_setup` rather than claiming a permission is live. The builder is refused
    its own slug on every write path, which is the lock that had been implicit while
    it could only create.
- **notification-relay**: not yet built.
- **Hosted sync store**: **built**, in `amber-infra/sync-store` — the server registry,
  Aperture's config blobs, and (new) the shared model-keyword table at `/models`.
  `/servers` is now actually *used* for discovery: Amber unions the store's peer list
  over `AMBER_MCP_PEERS` rather than reading only the env map, so registering an app
  makes it callable instead of merely visible. `AMBER_MCP_PEERS` stays the designed
  override — static beats discovered — and the whole fallback when no store is
  configured, not a workaround.
  Wiring a pair is `amber-infra/install/connect-peer.sh` (one button in Aperture's
  Servers tab), which reads the pairing out of both manifests, mints or reuses the
  bearer on the box, and publishes it to the store — so the two ends cannot be filled
  in with values that disagree.
- **amber-infra**: built out around the sync store (Caddy, install, deploy, backups).
  **amber-template**: specced, not yet built.
- **All individual app agents (finance, school, etc.)**: not yet started.
  Finance-agent is the intended first proof point, and is now unblocked — it can be
  built directly on `agent-mcp-py`. Per design principle 4 it ships backend + MCP
  server only; its screens are Aperture's job.
- **FreeCallMe MCP sidecar**: not started — intended as the proof point for the
  "compose external services, don't rewrite the app" pattern.
- **Aperture**: under way ahead of its planned slot, and now the centre of gravity
  rather than a late-stage nicety — Electron shell with the chat view, SSH/servers
  management, Bloom, and the Settings page that drives Amber's voice and model
  controls over the WS protocol.
  - **The visual pass has landed** (design principle 1). The chat is a *timeline*
    rather than a list of bubbles: messages and tool calls share one array, because
    their interleaving is the information. Tool calls render as collapsed cards with
    a live elapsed counter — a peer call may legitimately run for minutes, so a card
    with no visible progress is indistinguishable from one that has hung. A
    `bloom__build_agent` card expands into the **same** live build timeline the Bloom
    tab draws, which is what makes "build a spotify agent" watchable without leaving
    the Amber page. Replies render as markdown, from `delta`.
  - **Bloom's build view** replaces ~60–75 flat rows with a four-phase rail derived
    from the tool names (`bloom/build/phases.ts`, covered by `verify:activity`),
    collapsed per-phase groups, a capability grid built from the durable build row,
    and one cost strip instead of the twenty `step_finished` lines that all land at
    once. `TraceView` stays the single entry point so live and history cannot drift.
  - **The right sidebar** is an instrument stack, not a log: a pinned summary
    (connection, runs in flight, spend), then Running / Memory / System / Spend, with
    the old trace demoted to a filterable bottom section. Memory facts show tier,
    confidence and use count with a one-click forget and a real undo — Amber's delete
    is soft, so restoring brings back the same row. Resizable, and sections narrow
    themselves per view rather than sitting open and empty.
  - **It receives what Amber says unprompted.** `PushFrame` and `ConfirmRequestFrame`
    in `src/shared/protocol.ts`, a `PushTray` for the first and a modal `ConfirmDialog`
    for the second — a modal because a `confirm_request` has a *turn* stopped behind
    it, where a push does not. Two details are deliberate: pushes are deduped on `id`
    (delivery is at-least-once), and the dialog focuses **Deny**, treats Escape as a
    refusal, and shows no countdown — a timer would imply the safe outcome is the one
    that needs hurrying, when it is the one that happens if you do nothing.
    `verify:push` guards the frame gate, because a frame added to the `ServerFrame`
    union and forgotten in `SERVER_FRAME_TYPES` is dropped at the socket with no error
    anywhere — `protocol.ts` and `store.ts` had no verify script until now.
  - **The visual and intelligence passes landed.** A turn's latency renders as a
    waterfall (`chat/turn-layout.ts`, guarded by `verify:waterfall`), the status
    sidebar gains a **Health** section over the `review` frames, memory facts show a
    decay countdown and a revision chain (`status/fact-lifecycle.ts`, guarded by
    `verify:lifecycle` — three rules there would each produce a confident false
    sentence if got wrong), `RegistryMap` lights an edge while Amber is calling that
    peer (no new frame: `Activity.origin` is already `peer:<name>`), and a reply
    carries a provenance row plus a **wrong?** button that saves it as an eval case.
  - Still open: the terminal-only operations (principle 2). Durable provenance is the
    other known gap — explaining *yesterday's* turn needs schema the exchange log
    doesn't have (no session id, positional user/assistant pairing, and the fact ids
    a turn used are discarded), so today's provenance is live-session only.

## Build order (current)

1. ~~`agent-mcp-py`~~ — done
2. ~~`agent-runtime`~~ — done
3. ~~Amber refactor~~ — done (OpenClaw out, brain on `agent-runtime`, own MCP server)
4. ~~Hosted config sync store~~ — done, as `amber-infra/sync-store`: `/servers`
   (registration + discovery), `/models` (the shared keyword table) and `/config`.
   Peers resolve through `/servers` first-class now (`app/peers.py`), falling back to
   the static `AMBER_MCP_PEERS` map when no store is configured — and preferring it
   when both have an answer.
5. **Aperture** — promoted out of last place. It's under way, and the design
   principles make it the surface every later item is consumed through, so it stops
   being the thing that waits until the backends are done. Its first job list: Amber
   tools for her own settings (principle 3), a UI path for every current SSH-only
   operation (principle 2), and visual renderings of what the chat view currently
   prints as text (principle 1).
6. ~~`agent-spawner`~~ — done, as `bloom`
7. `finance-agent` as first full proof point — backend + MCP server only, rendered in
   Aperture. Worth deciding first whether it's a repo at all or a Bloom agent with a
   connection, now that Bloom makes the second cheap
8. FreeCallMe MCP sidecar
9. Remaining app agents and the shared design system — roughly in that order, but not
   strictly blocking each other. Per-app frontends are deliberately absent from this
   list; they get reconsidered only when an app clearly outgrows what Aperture and
   Amber give it.
