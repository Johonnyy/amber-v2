# Amber

A cloud-hosted personal AI voice backend — a persistent, always-available voice
agent with no UI of its own. Clients (an earpiece, a Pi, a browser tab) only record
and play audio; Amber is the intelligence behind all of them.

See [CLAUDE.md](CLAUDE.md) for the full design spec and the ecosystem context.

**All five phases are built** — voice pipeline, LLM brain + conversation, persistent
memory, tools, and polish/reliability — and the ecosystem refactor has landed: the
agentic loop now comes from the shared `agent-runtime` library, and Amber exposes
her own MCP server via `agent-mcp-py` so other agents can query her.

## The voice loop

```
client records audio → WS → STT (Whisper) → brain → sentence splitter
  → TTS (OpenAI) → WS → client plays, sentence by sentence
```

Audio streams back **sentence by sentence**: the splitter sits between the
response stream and TTS so the first sentence plays before the whole reply exists.
This is the performance-critical seam, and nothing in the refactor moved it — the
brain still hands downstream an `AsyncIterator[str]`, and tool round trips happen
invisibly inside it.

## Two directions

Amber both **calls** and **is called**:

```
          voice client ──WS──►  /ws   ─► brain ─► agent-runtime ─► OpenRouter
                                              └─► tools ─┬─► inline (app/tools)
                                                         └─► peer MCP servers
  spawner / Aperture ──HTTP──►  /mcp  ─► Amber's MCP server (memory, tasks)
                                /agent/usage
```

Heavy or delegated work used to go over an HTTP bridge to OpenClaw. That bridge is
gone; the same distinction now runs through peer MCP servers configured in
`AMBER_MCP_PEERS`.

## Layout

| Path | Role |
|------|------|
| [app/config.py](app/config.py) | All tiers / keys / flags (pydantic-settings, `AMBER_` prefix) |
| [app/protocol.py](app/protocol.py) | WebSocket wire contract (stable, public) |
| [app/sentence_splitter.py](app/sentence_splitter.py) | Streaming splitter — the perf-critical seam |
| [app/stt.py](app/stt.py) | Whisper transcription |
| [app/tts.py](app/tts.py) | OpenAI TTS (per-sentence) |
| [app/brain.py](app/brain.py) | The brain — wires `agent-runtime` to Amber's tools |
| [app/persona.py](app/persona.py) | System prompt + memory block composition |
| [app/responder.py](app/responder.py) | Canned fallback when `AMBER_FEATURE_LLM=false` |
| [app/memory/](app/memory/) | SQLite store, fact writer, context builder |
| [app/tools/](app/tools/) | Inline tools + the registry the brain and MCP server share |
| [app/mcp_server.py](app/mcp_server.py) | Amber's own MCP server (`agent-mcp-py`) |
| [app/session.py](app/session.py) | Per-connection history, session manager |
| [app/pipeline.py](app/pipeline.py) | The voice loop |
| [app/main.py](app/main.py) | FastAPI app, `/ws`, `/mcp`, `/agent/usage` |
| [deploy/](deploy/) | systemd unit + VPS setup |
| [scripts/smoke_client.py](scripts/smoke_client.py) | Manual end-to-end client |

## Shared libraries

Two sibling repos, pinned by commit in [pyproject.toml](pyproject.toml):

- **`agent-runtime`** — the agentic loop (call model → tool call → execute →
  repeat), streaming, cost tracking, and the model-tier router. Amber's brain is a
  thin wrapper over it, which is why `anthropic` is no longer a dependency.
- **`agent-mcp-py`** — the convention layer for Amber's own MCP server: bearer
  auth, the conversation-depth guard, per-call usage logging, sync-store
  registration.

**They are configured by injection, not environment.** Both would happily read
`AGENT_RUNTIME_*` / `AGENT_MCP_*` variables; Amber builds their settings objects
from `app/config.py` instead, so there is one prefix and no chance of two sources
disagreeing about — most damagingly — which database to write.

Model choice is a **named tier** (`AMBER_LLM_TIER=balanced`) resolved by
`agent_runtime.model_router`, not a model id. Upgrading the model every app uses is
one edit in the router. Today `balanced` is Claude Haiku, which is what Amber was
pinned to before the indirection existed.

## One database, three writers

`amber.db` holds Amber's memory (`facts`, `conversations`, `tasks`, `reminders`),
the MCP layer's tool log (`agent_mcp_usage`), and the runtime's model spend
(`agent_runtime_usage`). WAL plus a busy timeout makes the three-way tenancy safe,
and the shared `conversation_id` / `app_name` / `depth` / `created_at` columns mean
spend can be joined to the calls that caused it.

## Local development

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  use .venv/bin/activate on Linux/macOS
pip install -e ".[dev]"

cp .env.example .env            # then set AMBER_OPENAI_API_KEY + AMBER_OPENROUTER_API_KEY

# run the server
uvicorn app.main:app --reload

# run the tests (no network / API key needed — STT, TTS and the model are faked)
pytest

# prove the pipe end to end against the running server (needs real API keys)
python scripts/smoke_client.py path/to/utterance.wav
```

`GET /health` returns liveness. The voice endpoint is `ws://HOST:PORT/ws`.

## Client protocol (summary)

Every client speaks this; see [app/protocol.py](app/protocol.py) for exact shapes.

- **Send** a binary frame = one recorded utterance, or `{"type":"user_text","text":…}`
  when you already have the words.
- **Send** `{"type":"interrupt"}` = stop Amber mid-reply. Sending new audio while
  Amber is speaking also barges in (cancels the current turn).
- **Send** control frames to change things: `set_voice`, `set_model`,
  `register_tools`, `tool_result`, `memory_action`, `memory_query`, `push_ack`,
  `confirm_response`.
- **Receive** JSON control frames interleaved with binary audio frames. Each
  `audio_chunk` is immediately followed by the binary audio for that sentence.
  - Per turn: `transcript`, `thinking`, `memory`, `activity`, `delta`,
    `audio_chunk`, `turn_complete`, `error`.
  - On connect: `ready`, then `voice`, `model` and `status` — the server says what is
    true rather than the client shipping a copy that drifts.
  - `tool_call` asks the client to run one of its own declared tools.

Two frames Amber **originates**, rather than sending in reply to something:

- `push` — a fired reminder, a maintenance note, something that finished elsewhere.
  Held in a durable outbox when nothing is connected and delivered on the next
  connect, so delivery is **at-least-once**: dedupe on `id`. Never sent mid-turn.
- `confirm_request` — Amber is about to run a tool that needs human approval and is
  blocked until a `confirm_response` arrives. **Silence is a refusal.**

Frames are added additively, and ignoring one is safe for all of them **except
`confirm_request`** — the only frame a client genuinely owes an answer to. A client
that ignores it doesn't break, but the turn stalls for `AMBER_CONFIRM_TIMEOUT_S`
before the tool is refused. Set `AMBER_FEATURE_CONFIRMATIONS=false` for clients that
will never answer, and read `status.features` to know which frames this install sends.

## Amber's MCP server

Mounted at `/mcp` when `AMBER_MCP_KEYS` is set — **without keys it is simply not
mounted**, because `agent-mcp-py` fails closed and a default install should not
expose an open server.

| Kind | URI / name | |
|---|---|---|
| resource | `amber://memory/facts{?limit}` | distilled facts, newest first |
| resource | `amber://tasks/open` | open tasks, oldest first |
| resource | `amber://reminders/pending` | undelivered reminders |
| resource | `amber://memory/conversations{?limit}` | recent logged exchanges |
| tool | `search_memory` | read-only |
| tool | `list_tasks` | read-only |
| tool | `add_task` / `complete_task` | mutating |

`GET /agent/usage` returns the per-tool / per-caller summary the spawner reads.

The tools dispatch through the **same** `app/tools` registry the brain uses, so a
task added by a peer agent and one added by voice take the identical code path.

Callers present `Authorization: Bearer <token>`; use the `name:token` form in
`AMBER_MCP_KEYS` so usage rows record who called. Address `/mcp` or `/mcp/` — both
work.

## Deploy

See [deploy/README.md](deploy/README.md) — clone to `/opt/amber`, create a venv,
fill in `.env`, install the systemd unit, `systemctl enable --now amber`.
