"""Amber's own MCP server — the direction she couldn't face before.

Driven in-process via `mcp.Client`, so there is no socket and no network. The
transport, auth gate and depth guard belong to `agent-mcp-py` and are tested there;
what matters here is that Amber exposes the right things, backed by the *same*
store and the *same* tool registry the voice path uses.
"""

import pytest
from mcp import Client

import app.mcp_server as mcp_server
import app.tools.tasks as tasks_module
from app.config import Settings
from app.memory.store import MemoryStore


def _settings(**over):
    base = dict(
        feature_mcp_server=True,
        mcp_keys="spawner:tok",
        memory_db_path=":memory:",
        mcp_sync_store_url="",
        mcp_public_url="",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def store(monkeypatch):
    """An isolated store, installed everywhere the server reaches for one.

    Both bindings are needed: the tool modules did ``from app.memory.store import
    get_store`` at import time, so patching the store module alone would leave
    ``add_task`` writing to the real database — which is exactly the bug this
    fixture exists to prevent, and one a passing test would have hidden.
    """
    s = MemoryStore(":memory:")
    monkeypatch.setattr(mcp_server, "get_store", lambda: s)
    monkeypatch.setattr(tasks_module, "get_store", lambda: s)
    yield s
    s.close()


@pytest.fixture
def server(store):
    mcp_server.get_mcp_server.cache_clear()
    yield mcp_server.build_server(_settings())
    mcp_server.get_mcp_server.cache_clear()


# --- what is exposed ---


def test_the_expected_resources_and_tools_are_registered(server):
    uris = {r.uri for r in server.resource_policies()}
    assert uris == {
        "amber://memory/facts{?limit}",
        "amber://tasks/open",
        "amber://reminders/pending",
        "amber://memory/conversations{?limit}",
    }
    assert {t.name for t in server.tool_policies()} == {
        "search_memory",
        "list_tasks",
        "add_task",
        "complete_task",
    }


def test_query_tools_are_marked_read_only(server):
    policies = {t.name: t for t in server.tool_policies()}
    assert policies["search_memory"].read_only is True
    assert policies["list_tasks"].read_only is True
    assert policies["add_task"].read_only is False
    assert policies["complete_task"].read_only is False


async def test_the_advertised_schemas_are_wire_safe(server):
    """agent_runtime turns these into OpenAI tools; $ref/$defs would be rejected by
    several providers."""
    import json

    async with Client(server.mcp) as client:
        listed = await client.list_tools()
    text = json.dumps([t.model_dump(by_alias=True, mode="json") for t in listed.tools])
    assert "$ref" not in text and "$defs" not in text


# --- resources return what a client's panels would render ---


async def test_facts_resource_returns_stored_facts(server, store):
    store.add_fact("Drinks oat milk", category="preference")
    async with Client(server.mcp) as client:
        result = await client.read_resource("amber://memory/facts?limit=5")
    assert "oat milk" in result.contents[0].text


async def test_open_tasks_resource(server, store):
    store.add_task("call the dentist")
    async with Client(server.mcp) as client:
        result = await client.read_resource("amber://tasks/open")
    assert "dentist" in result.contents[0].text


async def test_pending_reminders_resource(server, store):
    store.add_reminder("stand up", remind_at="2026-08-14T09:00:00+00:00")
    async with Client(server.mcp) as client:
        result = await client.read_resource("amber://reminders/pending")
    assert "stand up" in result.contents[0].text


async def test_conversations_resource(server, store):
    store.log_exchange("hello", "hi there")
    async with Client(server.mcp) as client:
        result = await client.read_resource("amber://memory/conversations?limit=10")
    assert "hi there" in result.contents[0].text


# --- tools go through the same registry the brain uses ---


async def test_add_task_writes_to_the_same_store_the_voice_path_uses(server, store):
    """Not a parallel implementation: a task added by a peer agent and one added by
    voice take the identical code path."""
    async with Client(server.mcp) as client:
        result = await client.call_tool("add_task", {"description": "buy milk"})

    assert result.is_error is False
    assert [t["description"] for t in store.open_tasks()] == ["buy milk"]


async def test_complete_task_closes_it(server, store):
    task_id = store.add_task("water the plants")
    async with Client(server.mcp) as client:
        result = await client.call_tool("complete_task", {"task_id": task_id})
    assert result.is_error is False
    assert store.open_tasks() == []


async def test_list_tasks_reads_back(server, store):
    store.add_task("renew passport")
    async with Client(server.mcp) as client:
        result = await client.call_tool("list_tasks", {})
    assert "passport" in result.content[0].text


async def test_search_memory_finds_a_fact(server, store):
    store.add_fact("Allergic to penicillin")
    store.add_fact("Prefers window seats")
    async with Client(server.mcp) as client:
        result = await client.call_tool("search_memory", {"query": "penicillin"})
    text = result.content[0].text
    assert "penicillin" in text
    assert "window seats" not in text


async def test_search_memory_says_so_when_nothing_matches(server, store):
    async with Client(server.mcp) as client:
        result = await client.call_tool("search_memory", {"query": "nothing"})
    assert "No stored facts" in result.content[0].text


async def test_a_failing_tool_returns_an_error_result_not_a_crash(server, store):
    """Amber's rule survives the new surface: a bad tool never takes down the turn,
    and the caller gets text it can act on."""
    async with Client(server.mcp) as client:
        result = await client.call_tool("complete_task", {"task_id": 9999})
    # The registry converts the miss into a message rather than raising.
    assert result.content[0].text


# --- usage logging lands in Amber's own database ---


async def test_every_call_is_logged_with_ambers_app_name(server, store):
    async with Client(server.mcp) as client:
        await client.call_tool("list_tasks", {})

    rows = server._sink.rows()
    assert len(rows) == 1
    assert rows[0]["name"] == "list_tasks"
    assert rows[0]["app_name"] == "amber"
    assert rows[0]["ok"] == 1


# --- configuration comes from Amber, not the environment ---


def test_mcp_settings_are_injected_not_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_MCP_APP_NAME", "WRONG")
    monkeypatch.setenv("AGENT_MCP_USAGE_DB_PATH", "wrong.db")
    monkeypatch.setenv("AGENT_MCP_ALLOW_ANONYMOUS", "true")

    cfg = mcp_server.mcp_settings(_settings(memory_db_path="amber.db"))
    assert cfg.app_name == "amber"
    assert cfg.usage_db_path == "amber.db"  # shares the file with memory + costs
    assert cfg.allow_anonymous is False  # never open, whatever the environment says


def test_the_tool_log_shares_ambers_database():
    cfg = mcp_server.mcp_settings(_settings(memory_db_path="/tmp/amber.db"))
    assert cfg.usage_db_path == "/tmp/amber.db"
