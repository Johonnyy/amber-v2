"""Amber's own MCP server — making her queryable, not just a caller.

Until now Amber only ever reached *out*: she called tools, and later other agents'
servers. This module is the other direction. It exposes what she knows — the facts
she has accumulated, her task list, her pending reminders — as MCP resources and
tools, so `agent-spawner`, Aperture, or any other agent can ask her things without
going through the voice socket.

Built on `agent-mcp-py`, which is precisely the point: bearer auth, the
conversation-depth guard, per-call usage logging and sync-store registration all
arrive with the decorators rather than being reimplemented here.

Two conventions from the ecosystem spec shape what is exposed:

* **Resource URIs mirror real views.** ``amber://memory/facts`` returns the same
  facts the client's memory panel renders from — there is no separate agent-only
  version of the data that could drift from what a human sees.
* **Tools mirror real user actions, calling the same underlying function.**
  ``add_task`` here dispatches through `app.tools`, the identical registry the
  brain uses, so a task added by a peer agent and one added by voice take the same
  code path. A parallel implementation is exactly the thing this convention exists
  to prevent.

Everything is configured by injection from `app.config`, never from
``AGENT_MCP_*`` environment variables — Amber has one config surface.

**Note on `read_only=False` tools.** ``add_task`` and ``complete_task`` change
state but are not marked ``requires_confirmation``: they are trivially reversible
and Amber has no path to ask a human for approval yet (`app.protocol` has no
tool-event frame, so ``X-Confirmed`` has no source). Marking them would make them
permanently uncallable rather than safely gated. When that frame lands, revisit.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from agent_mcp import AgentMCPServer, AgentMCPSettings

from app.config import Settings, get_settings
from app.memory.store import get_store
from app.tools import run_tool

logger = logging.getLogger(__name__)


def mcp_settings(settings: Settings | None = None) -> AgentMCPSettings:
    """Build `agent_mcp`'s settings from Amber's own.

    ``_env_file=None`` so nothing is read from disk: every value comes from
    `app.config`, which is the only prefix this app has.
    """
    settings = settings or get_settings()
    return AgentMCPSettings(
        _env_file=None,
        app_name="amber",
        version="0.1.0",
        keys=settings.mcp_keys,
        allow_anonymous=False,
        public_url=settings.mcp_public_url,
        sync_store_url=settings.mcp_sync_store_url,
        sync_store_token=settings.mcp_sync_store_token,
        # The tool log shares Amber's database with her memory and agent-runtime's
        # cost rows. WAL and a busy timeout make the three-way tenancy safe, and the
        # shared conversation_id/app_name/depth/created_at columns let spend be
        # joined to the calls that caused it.
        usage_db_path=settings.memory_db_path,
        usage_enabled=True,
    )


def build_server(settings: Settings | None = None) -> AgentMCPServer:
    """Construct Amber's MCP server with every resource and tool registered.

    Not cached itself — see :func:`get_mcp_server` — so tests can build an isolated
    instance without clearing a singleton.
    """
    settings = settings or get_settings()
    mcp = AgentMCPServer(
        app_name="amber",
        version="0.1.0",
        settings=mcp_settings(settings),
        instructions=(
            "Amber is Johnny's personal voice agent. She holds distilled facts "
            "about him, his open tasks, and his pending reminders. Query her for "
            "personal context another agent would otherwise have to ask him for."
        ),
    )

    # --- resources: the same data a client's panels render ---

    @mcp.resource("amber://memory/facts{?limit}")
    def recent_facts(limit: int = 20) -> list[dict]:
        """Distilled facts Amber has learned about the user, newest first."""
        return get_store().recent_facts(limit)

    @mcp.resource("amber://tasks/open")
    def open_tasks() -> list[dict]:
        """Open tasks, oldest first — the order you'd work through them."""
        return get_store().open_tasks()

    @mcp.resource("amber://reminders/pending")
    def pending_reminders() -> list[dict]:
        """Reminders not yet delivered, oldest first."""
        return get_store().pending_reminders()

    @mcp.resource("amber://memory/conversations{?limit}")
    def recent_messages(limit: int = 20) -> list[dict]:
        """The most recent logged exchanges, oldest first."""
        return get_store().recent_messages(limit)

    @mcp.resource("amber://memory/reflections{?limit}")
    def recent_reflections(limit: int = 10) -> list[dict]:
        """What Amber's maintenance pass has noticed about how conversations go.

        The read-only window onto the self-review loop: these notes are written
        unattended, so being able to see them from outside is how you tell whether
        they're worth acting on.
        """
        return get_store().recent_reflections(limit)

    # --- tools: the same actions, through the same registry ---

    @mcp.tool(read_only=True)
    async def search_memory(query: str, limit: int = 10) -> str:
        """Search Amber's distilled facts about the user for a word or phrase."""
        # Dispatched through the registry rather than reimplemented here. This used
        # to be a private substring scan over every fact — a parallel code path that
        # was both worse (no ranking, no index) and free to drift from what Amber
        # herself sees when she searches her own memory.
        return await run_tool("search_memory", {"query": query, "limit": limit})

    @mcp.tool(read_only=True)
    async def list_tasks() -> str:
        """List the user's open tasks."""
        return await run_tool("list_tasks", {})

    @mcp.tool(read_only=False)
    async def add_task(description: str) -> str:
        """Add a to-do item to the user's task list."""
        return await run_tool("add_task", {"description": description})

    @mcp.tool(read_only=False)
    async def complete_task(task_id: int) -> str:
        """Mark one of the user's tasks as done."""
        return await run_tool("complete_task", {"task_id": task_id})

    logger.info(
        "Amber MCP server ready: %d tool(s), %d resource(s)",
        len(mcp.tool_policies()),
        len(mcp.resource_policies()),
    )
    return mcp


@lru_cache
def get_mcp_server() -> AgentMCPServer:
    """Process-wide MCP server singleton.

    Cached because `asgi_app()` must be built exactly once — two calls would create
    two session managers, and the lifespan would run the one that isn't serving
    traffic. Tests clear it with ``get_mcp_server.cache_clear()``.
    """
    return build_server()
