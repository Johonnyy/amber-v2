"""Speech-to-text via OpenAI Whisper.

The client sends a complete recorded utterance as raw audio bytes; we hand them to
the Whisper API and return the transcript. The model is config-driven
(`settings.stt_model`) so it can be swapped without touching call sites.
"""

from __future__ import annotations

import io
import logging

from app.config import get_settings
from app.openai_client import get_client

logger = logging.getLogger(__name__)


async def transcribe(audio: bytes, *, filename: str = "audio.webm") -> str:
    """Transcribe an utterance to text.

    ``audio`` is the raw bytes received over the WebSocket. ``filename`` only needs
    a valid extension so the API can infer the container (webm/wav/mp3/m4a/ogg…);
    clients should send a format Whisper accepts.
    """
    settings = get_settings()
    client = get_client()

    buffer = io.BytesIO(audio)
    buffer.name = filename  # the SDK uses the name to detect the upload format

    logger.debug("STT: %d bytes -> %s", len(audio), settings.stt_model)
    result = await client.audio.transcriptions.create(
        model=settings.stt_model,
        file=buffer,
    )
    text = (result.text or "").strip()
    logger.debug("STT transcript: %r", text)
    return text
