"""Manual smoke-test client for the Amber voice pipe.

Sends an audio file as one utterance and saves each sentence of the spoken reply.
Proves the full pipe (STT -> think -> sentence-streamed TTS) against a running
server without needing to build a real client.

Usage:
    python scripts/smoke_client.py path/to/utterance.wav
    python scripts/smoke_client.py path/to/utterance.wav --url ws://1.2.3.4:8000/ws --token SECRET

Requires the `websockets` package (installed transitively via uvicorn[standard]).
Reply audio is written to ./out/reply_000.<fmt>, reply_001.<fmt>, ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib

import websockets


async def run(audio_path: str, url: str, token: str | None) -> None:
    if token:
        url = f"{url}?token={token}"
    out_dir = pathlib.Path("out")
    out_dir.mkdir(exist_ok=True)

    data = pathlib.Path(audio_path).read_bytes()

    async with websockets.connect(url, max_size=None) as ws:
        print(f"connected; sending {len(data)} bytes from {audio_path}")
        # wait for ready
        print("server:", await ws.recv())
        await ws.send(data)

        pending_meta: dict | None = None
        while True:
            message = await ws.recv()
            if isinstance(message, bytes):
                idx = pending_meta["index"] if pending_meta else 0
                fmt = pending_meta["format"] if pending_meta else "bin"
                path = out_dir / f"reply_{idx:03d}.{fmt}"
                path.write_bytes(message)
                print(f"  saved {path} ({len(message)} bytes)")
                pending_meta = None
                continue

            frame = json.loads(message)
            kind = frame.get("type")
            if kind == "transcript":
                print("transcript:", frame["text"])
            elif kind == "audio_chunk":
                pending_meta = frame
                print(f"sentence[{frame['index']}]: {frame['text']!r}")
            elif kind == "turn_complete":
                print(f"turn complete: {frame['sentences']} sentence(s)")
                break
            elif kind == "error":
                print("ERROR:", frame["message"])
                break
            else:
                print("server:", frame)


def main() -> None:
    parser = argparse.ArgumentParser(description="Amber voice-pipe smoke client")
    parser.add_argument("audio", help="path to an audio file (wav/mp3/m4a/webm/ogg)")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    parser.add_argument("--token", default=None, help="auth token if AMBER_AUTH_SECRET is set")
    args = parser.parse_args()
    asyncio.run(run(args.audio, args.url, args.token))


if __name__ == "__main__":
    main()
