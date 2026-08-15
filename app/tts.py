"""Text-to-speech via OpenAI.

Synthesis happens one sentence at a time: the voice loop feeds each sentence from
the splitter here as soon as it's ready, and the resulting audio bytes are streamed
straight back to the client. Keeping synthesis at sentence granularity is what lets
the first audio play before the whole response exists.

Which voice, which model, how fast, and in what container are all carried by a
`app.voice.VoiceSettings` value rather than read off `settings` here — the pipeline
passes the *connection's* voice down, so two clients can sound different and either
can change mid-session. Omitting it falls back to the install default, so every
existing call site behaves exactly as before.
"""

from __future__ import annotations

import logging

from app.openai_client import get_client
from app.voice import VoiceSettings

logger = logging.getLogger(__name__)


async def synthesize(text: str, voice: VoiceSettings | None = None) -> bytes:
    """Render a single sentence to audio and return the encoded bytes.

    The container is ``voice.audio_format`` (e.g. mp3); the matching ``audio_chunk``
    metadata frame tells the client how to decode it.
    """
    settings = voice or VoiceSettings.from_settings()
    client = get_client()

    params = settings.speech_params()
    logger.debug("TTS: %r -> %s/%s @%sx", text, params["model"], params["response_format"], settings.speed)
    async with client.audio.speech.with_streaming_response.create(
        input=text, **params
    ) as response:
        chunks = [chunk async for chunk in response.iter_bytes()]
    return b"".join(chunks)
