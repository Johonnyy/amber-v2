"""OpenClaw bridge — delegate heavy work to the OpenClaw backend (Phase 4).

The load-bearing distinction in Amber's tool design: inline tools are fast and
local to Amber; **anything heavier** — calendar, email, files, the browser — is
delegated to OpenClaw, a separate service running on another host.
``delegate_to_openclaw`` sends a natural-language task description over HTTP and
**blocks until OpenClaw returns**, then hands the result back to the model to weave
into its spoken reply.

Connection is config-driven: ``AMBER_OPENCLAW_URL`` (the host/gateway) and
``AMBER_OPENCLAW_TOKEN`` (the gateway/bearer token, sent as ``Authorization:
Bearer ...``). The tool is only offered to the model when a URL is configured —
``available`` hides it otherwise — so Amber never claims a capability it can't use.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _configured() -> bool:
    """OpenClaw is usable only once a backend URL is set."""
    return bool(get_settings().openclaw_url)


def _extract_result(data: object) -> str:
    """Pull a human-readable result out of OpenClaw's response, liberally."""
    if isinstance(data, dict):
        for key in ("result", "response", "output", "message"):
            value = data.get(key)
            if value:
                return str(value)
        return str(data)
    return str(data)


@registry.register(
    name="delegate_to_openclaw",
    description=(
        "Delegate a heavier task to OpenClaw, the user's separate automation "
        "backend: calendar, email, files, web browsing, or any multi-step action "
        "Amber can't do inline. Describe the task in plain language. This blocks "
        "until OpenClaw finishes and returns its result, so prefer the lightweight "
        "inline tools for quick lookups and simple tasks."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "The task for OpenClaw, in natural language.",
            }
        },
        "required": ["task_description"],
    },
    available=_configured,
)
async def delegate_to_openclaw(task_description: str) -> str:
    task_description = (task_description or "").strip()
    if not task_description:
        return "Error: nothing to delegate (empty task)."

    settings = get_settings()
    if not settings.openclaw_url:
        return "OpenClaw isn't configured (set AMBER_OPENCLAW_URL)."

    url = settings.openclaw_url.rstrip("/") + "/task"
    headers = {}
    if settings.openclaw_token:
        headers["Authorization"] = f"Bearer {settings.openclaw_token}"

    try:
        async with httpx.AsyncClient(timeout=settings.openclaw_timeout_s) as client:
            resp = await client.post(
                url, json={"task": task_description}, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("OpenClaw delegation failed: %s", exc)
        return f"Couldn't reach OpenClaw right now ({exc})."

    return _extract_result(data)
