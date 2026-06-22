# Amber

A cloud-hosted personal AI voice backend — a persistent, always-available voice
agent with no UI of its own. Clients (an earpiece, a Pi, a browser tab) only record
and play audio; Amber is the intelligence behind all of them.

See [CLAUDE.md](CLAUDE.md) for the full design spec and phase plan.

**This repo currently implements Phase 1: skeleton & voice pipeline.** There's no
LLM brain yet (Phase 2) — Amber transcribes what you say and speaks back a canned
greeting, which proves the end-to-end pipe and exercises the sentence-streaming seam.

## The voice loop

```
client records audio → WS → STT (Whisper) → think → sentence splitter
  → TTS (OpenAI) → WS → client plays, sentence by sentence
```

Audio streams back **sentence by sentence**: the splitter sits between the
response stream and TTS so the first sentence plays before the whole reply exists.

## Layout

| Path | Role |
|------|------|
| [app/config.py](app/config.py) | All models / keys / flags (pydantic-settings) |
| [app/protocol.py](app/protocol.py) | WebSocket wire contract (stable, public) |
| [app/sentence_splitter.py](app/sentence_splitter.py) | Streaming splitter — the perf-critical seam |
| [app/stt.py](app/stt.py) | Whisper transcription |
| [app/tts.py](app/tts.py) | OpenAI TTS (per-sentence) |
| [app/responder.py](app/responder.py) | Phase-1 canned "brain" (replaced in Phase 2) |
| [app/pipeline.py](app/pipeline.py) | The voice loop |
| [app/main.py](app/main.py) | FastAPI app + `/ws` endpoint + interrupt handling |
| [deploy/](deploy/) | systemd unit + VPS setup |
| [scripts/smoke_client.py](scripts/smoke_client.py) | Manual end-to-end client |

## Local development

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  use .venv/bin/activate on Linux/macOS
pip install -e ".[dev]"

cp .env.example .env            # then set AMBER_OPENAI_API_KEY

# run the server
uvicorn app.main:app --reload

# run the tests (no network / API key needed — STT & TTS are faked)
pytest

# prove the pipe end to end against the running server (needs a real API key)
python scripts/smoke_client.py path/to/utterance.wav
```

`GET /health` returns liveness. The voice endpoint is `ws://HOST:PORT/ws`.

## Client protocol (summary)

Every client speaks this; see [app/protocol.py](app/protocol.py) for exact shapes.

- **Send** a binary frame = one recorded utterance.
- **Send** `{"type":"interrupt"}` = stop Amber mid-reply. Sending new audio while
  Amber is speaking also barges in (cancels the current turn).
- **Receive** JSON control frames (`ready`, `transcript`, `thinking`,
  `audio_chunk`, `turn_complete`, `error`) interleaved with binary audio frames.
  Each `audio_chunk` frame is immediately followed by the binary audio for that
  sentence.

## Deploy

See [deploy/README.md](deploy/README.md) — clone to `/opt/amber`, create a venv,
fill in `.env`, install the systemd unit, `systemctl enable --now amber`.
