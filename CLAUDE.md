# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: greenfield

As of this writing the repository is **empty** — no code, dependencies, or build tooling exist yet. This file is the design spec, not a description of existing code. The first task is Phase 1 (skeleton & voice pipeline). When you scaffold the project, update the **Commands** section below with the real invocations you create.

Stack is Python: FastAPI + WebSocket server, deployed to an OVH VPS under systemd. There is no second repo here — the OpenClaw backend is a separate service Amber talks to over HTTP.

## What Amber is

A cloud-hosted personal AI backend — a persistent, always-available voice agent with no UI of its own. Everything the user touches (an earpiece, a Pi with a screen, a browser tab) is a thin **client**; Amber is the intelligence behind all of them.

It is **not** a chatbot UI, not OpenClaw, not a local model, not a configurable framework. It is a codebase you own.

## Core architecture

### The voice loop (the entire contract)

```
client records audio → WS → Amber transcribes (Whisper/OpenAI STT)
  → Amber thinks (LLM call, with memory injected as system context)
  → LLM stream → sentence splitter → TTS (OpenAI) → WS → client plays
```

The client only records and plays. Audio streams back **sentence by sentence** — the sentence splitter sits between the LLM token stream and TTS so the first audio plays before the full response is generated. This streaming boundary is the performance-critical seam; keep it intact when modifying the pipeline.

### Client protocol (WebSocket)

Every client speaks the same protocol — building a new client means writing a thin wrapper around it:
- **Send:** raw audio.
- **Receive:** streamed audio + optional metadata (transcript, thinking state, tool events).
- **Interrupt:** client sends an interrupt message → Amber stops speaking mid-response.

Treat this protocol as a stable public contract. Changing message shapes breaks every client.

### Memory (the thing that makes it smart)

Amber builds a persistent picture of the user across all conversations — facts, preferences, ongoing tasks, noticed patterns. Stored in SQLite (`facts`, `conversations`, `tasks`).

Two halves:
- **Writer:** after each exchange, extract distilled facts worth keeping. Store *punchy distilled facts, not raw transcripts.*
- **Context builder:** on each new message, pull relevant memory into the system prompt as a **compressed** block.

Keep memory small and high-signal — every LLM call pays for it in tokens. Bloating it degrades both cost and quality.

### Tools

- **Inline tools** (lightweight, run during a response via LLM tool-use): web search, task add/list/complete, set reminder/timer. Defined with a tool-registry pattern — Python functions with schemas; the LLM decides which to call.
- **OpenClaw bridge** for anything heavier (calendar, email, files, browser): `delegate_to_openclaw(task_description)` sends a natural-language request over HTTP and **blocks until the result returns** before continuing the response.

The distinction is load-bearing: inline = fast/local to Amber; OpenClaw = heavy/delegated/awaited.

### Session model

Conversation history is maintained **per WebSocket connection, in-memory** (Phase 2). Persistent cross-session knowledge lives only in memory (SQLite), not in conversation history. Don't conflate the two.

## Phase plan (build order)

Work in this order; each phase assumes the previous one works.

1. **Skeleton & voice pipeline** — FastAPI + WebSocket; accept raw audio and return a TTS "hello" to prove the pipe; Whisper STT (OpenAI API); OpenAI TTS with sentence-level streaming; config system (model choices, API keys, feature flags); deploy to OVH VPS with systemd.
2. **LLM + conversation** — Claude Haiku as the brain; system prompt for Amber's personality/role; per-connection in-memory history; streaming LLM → sentence splitter → TTS → client; basic interrupt handling.
3. **Memory** — SQLite schema (facts/conversations/tasks); memory writer (fact extraction after each exchange); context builder (relevant memory into system prompt).
4. **Tools** — tool registry; inline tools (web search, tasks, reminders); OpenClaw bridge.
5. **Polish & reliability** — session management (reconnect, session IDs); client auth (shared secret to start); logging & error recovery; rate limiting & cost guardrails.

## Config

All model choices, API keys, and feature flags go through the config system (Phase 1). Don't hardcode model names or keys inline — route them through config so the brain (Claude Haiku), STT, and TTS models are swappable.

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

Deploy to the OVH VPS via systemd — see [deploy/README.md](deploy/README.md). The
unit is `deploy/amber.service`; runs `uvicorn app.main:app` under user `amber` from
`/opt/amber`, config from `/opt/amber/.env`. Update: `git pull` then
`systemctl restart amber`.

Key modules: `app/config.py` (all models/keys/flags), `app/protocol.py` (WS wire
contract), `app/sentence_splitter.py` (streaming seam), `app/pipeline.py` (the voice
loop), `app/main.py` (FastAPI + `/ws`). The "brain" is `app/brain.py` (streamed
Claude Haiku) with its personality in `app/persona.py` (`compose_system_prompt`
appends the memory block); `app/session.py` holds per-connection conversation
history. `app/responder.py` is the canned fallback used when `AMBER_FEATURE_LLM=
false`. Both speak the same `AsyncIterator[str]` contract, so the pipeline
downstream of the brain is unchanged.

Persistent memory (Phase 3) lives in the `app/memory/` package: `store.py` (SQLite
`facts`/`conversations`/`tasks`/`reminders` tables + sync CRUD, `get_store()`),
`writer.py` (`remember` — distil facts from an exchange via a cheap LLM call, after
the turn is spoken), and `context.py` (`build_memory_view` — rank relevant facts in
one store pass into both a compressed prompt *block* for the system prompt and a
flat list of *items* for client display; `build_context` is the block-only
wrapper). Gated by `AMBER_FEATURE_MEMORY`; the read half runs inline before the
brain — injecting the block into the prompt *and* emitting an additive `memory`
protocol frame (the same facts, advisory, for the client's memory panel) — and the
write half runs off the latency path after `turn_complete`. Memory is *persistent
cross-session knowledge*, distinct
from the in-memory per-connection history in `app/session.py` — don't conflate them.

Tools (Phase 4) live in the `app/tools/` package, gated by `AMBER_FEATURE_TOOLS`.
`registry.py` is the pattern: `@registry.register(name, description, input_schema)`
decorates a Python function (sync or async) returning a result string; `schemas()`
exports the Anthropic `tools=[...]` list and `dispatch()` runs a call, converting
any error into a string so a bad tool never crashes a turn. A tool may carry an
`available()` predicate — unavailable tools are hidden and refuse to run. Inline
tools: `search.py` (`web_search`; provider `duckduckgo` keyless / `tavily`),
`tasks.py` (`add_task`/`list_tasks`/`complete_task` over the store), `reminders.py`
(`set_reminder` — persists to the `reminders` table; firing/delivery is future
work). The OpenClaw bridge is `openclaw.py` (`delegate_to_openclaw` — POSTs a
natural-language task to `AMBER_OPENCLAW_URL` with a bearer `AMBER_OPENCLAW_TOKEN`
and blocks for the result; only offered when a URL is set). The agentic loop —
stream → `tool_use` → execute → feed results back → repeat to `max_tool_iterations`
— lives in `app/brain.py`; it works on a copy of the history so only spoken text is
recorded, leaving everything downstream of the brain unchanged.

Reliability (Phase 5) is concentrated in the transport layer. `app/session.py` now
holds, besides `Conversation` (the per-turn history, capped by `max_history_turns`),
a `Session` (stable id + retained conversation + per-session limiter) and a
`SessionManager` (`get_session_manager()`): it mints a session id, keeps a dropped
connection's history warm for `session_ttl_s` so a reconnect with `?session_id=`
resumes, and evicts by TTL / `max_sessions`. `app/ratelimit.py` is a sliding-window
`RateLimiter` (one per session). `app/main.py` ties it together: auth via
`AMBER_AUTH_SECRET` (`?token=` or `Authorization: Bearer`), the session handshake
(id returned in `ready`), and `_admit_utterance` — the cost guardrails
(`max_audio_bytes`, `rate_limit_turns`/`window`, `max_turns_per_session`) that
reject an utterance with a coded `error` frame *before* any STT/LLM/TTS spend. A
failed turn becomes an `error` frame, never a dropped socket; logs are tagged with
the session id. Protocol changes are additive (new `ready.session_id`, optional
`error.code`), so existing clients keep working.
