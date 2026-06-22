"""Text-to-speech via OpenAI.

Synthesis happens one sentence at a time: the voice loop feeds each sentence from
the splitter here as soon as it's ready, and the resulting audio bytes are streamed
straight back to the client. Keeping synthesis at sentence granularity is what lets
the first audio play before the whole response exists.

Model, voice, and output container are all config-driven so they're swappable.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.openai_client import get_client

logger = logging.getLogger(__name__)


async def synthesize(text: str) -> bytes:
    """Render a single sentence to audio and return the encoded bytes.

    The container is `settings.tts_format` (e.g. mp3); the matching metadata frame
    tells the client how to decode it.
    """
    settings = get_settings()
    client = get_client()

    logger.debug("TTS: %r -> %s/%s", text, settings.tts_model, settings.tts_format)
    async with client.audio.speech.with_streaming_response.create(
        model=settings.tts_model,
        voice=settings.tts_voice,
        input=text,
        response_format=settings.tts_format,
    ) as response:
        chunks = [chunk async for chunk in response.iter_bytes()]
    return b"".join(chunks)
