"""Peer discovery — the layer `build_broker` was built to ignore.

The bug these cover had no symptom, which is why it needs tests rather than a fix.
Amber's broker was assembled like this::

    peers = load_static_peers(settings.mcp_peers, settings.mcp_peer_token)
    if peers:
        brokers.append(MCPClient(list(peers), resolver=peers))

With ``AMBER_MCP_PEERS`` empty, no MCP client was constructed at all — so Bloom could
register with the sync store, appear in ``GET /servers``, mount its MCP server and
answer 401 to an unauthenticated probe, while Amber named her own thirteen tools and
no ``bloom__*`` ones. Nothing logged anything, because an unlisted peer is not a tool
that fails; it is no tool. And even with a static map, passing that dict as
``resolver`` made `MCPClient._resolve` take its ``Mapping`` branch and return before
it could ever consult `agent_mcp.registry`, so the discovered layer was unreachable
either way.

Both halves are asserted below, because either one alone reproduces the outage.
"""

import asyncio

import pytest
from agent_mcp.registry import PeerRecord, default_registry
from agent_mcp import registry as agent_registry

from app import brain, peers
from app.config import Settings


def _settings(**over):
    base = dict(
        feature_tools=True,
        feature_peer_discovery=True,
        llm_tier="balanced",
        llm_max_tokens=256,
        max_tool_iterations=4,
        memory_db_path=":memory:",
        openrouter_api_key="sk-test",
        mcp_peers="",
        mcp_sync_store_url="https://sync.test",
        mcp_sync_store_token="store-token",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def clean_registry():
    """Both layers of the process-wide registry, restored around every test.

    `app.peers.registry()` deliberately returns `agent_mcp`'s module-level singleton
    rather than one of Amber's — a second registry would be a second answer to "where
    is bloom". The cost of that correctness is exactly this: it is shared state, and a
    test that seeds a peer would otherwise leak it into
    ``test_no_peers_means_no_mcp_client`` in the neighbouring file and make it fail
    for a reason that has nothing to do with what it is testing.
    """
    reg = default_registry()
    static, discovered = dict(reg._static), dict(reg._discovered)
    yield reg
    reg._static, reg._discovered = static, discovered


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for `httpx2.AsyncClient` inside `PeerRegistry.refresh`.

    Patched at the transport rather than over `refresh` itself, so these exercise the
    library's real parsing of the store's envelope — which is the half of the contract
    Amber does not own and could silently stop matching.
    """

    calls: list[tuple[str, dict]] = []
    payload: object = {"servers": []}
    error: Exception | None = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        type(self).calls.append((url, dict(headers or {})))
        if type(self).error is not None:
            raise type(self).error
        return _FakeResponse(type(self).payload)


@pytest.fixture
def store(monkeypatch):
    """A fake sync store, seeded per test."""
    _FakeClient.calls = []
    _FakeClient.payload = {"servers": []}
    _FakeClient.error = None
    monkeypatch.setattr(agent_registry.httpx2, "AsyncClient", _FakeClient)
    return _FakeClient


# --- the regression ---------------------------------------------------------


async def test_a_registered_peer_is_callable_with_an_empty_static_map(store):
    """THE bug. An empty AMBER_MCP_PEERS used to mean no MCP client at all."""
    store.payload = {
        "servers": [{"name": "bloom", "base_url": "https://bloom.test", "token": "peer-tok"}]
    }
    await peers.refresh_once(_settings(mcp_peers=""))

    broker = brain.build_broker(_settings(mcp_peers=""))

    assert isinstance(broker, brain.CompositeBroker)
    client = broker.brokers[-1]
    assert isinstance(client, brain.MCPClient)
    assert client.servers == ["bloom"]


def test_no_peers_anywhere_still_means_no_mcp_client():
    """The other side of it: discovery must not invent a client out of nothing."""
    assert isinstance(brain.build_broker(_settings()), brain.AnthropicRegistryBroker)


async def test_the_resolver_is_the_registry_not_a_frozen_dict(store):
    """The second half of the bug, which a static map alone would have hidden.

    Passing the peer dict as ``resolver`` made MCPClient._resolve return from its
    Mapping branch, so the record was whatever it was at build time and the sync
    store could never correct it. Re-pointing a peer between turns proves the lookup
    is live.
    """
    store.payload = {"servers": [{"name": "bloom", "base_url": "https://old.test"}]}
    await peers.refresh_once(_settings())
    client = brain.build_broker(_settings()).brokers[-1]

    store.payload = {"servers": [{"name": "bloom", "base_url": "https://new.test"}]}
    await peers.refresh_once(_settings())

    assert client._resolve("bloom")["base_url"] == "https://new.test"


async def test_the_endpoint_carries_the_mount_path_and_a_trailing_slash(store):
    """A base URL is stored; /mcp/ is what gets opened. Off by one and it is a 404."""
    store.payload = {"servers": [{"name": "bloom", "base_url": "https://bloom.test"}]}
    await peers.refresh_once(_settings())
    client = brain.build_broker(_settings()).brokers[-1]

    record = client._resolve("bloom")
    assert client._endpoint(record, "bloom") == "https://bloom.test/mcp/"


async def test_the_discovered_token_reaches_the_record(store):
    """What makes discovery a complete answer: the store carries the credential too,
    so a discovered peer needs no AMBER_MCP_PEER_TOKEN."""
    store.payload = {
        "servers": [{"name": "bloom", "base_url": "https://bloom.test", "token": "peer-tok"}]
    }
    await peers.refresh_once(_settings())

    assert brain.build_broker(_settings()).brokers[-1]._resolve("bloom")["token"] == "peer-tok"


# --- precedence -------------------------------------------------------------


async def test_static_configuration_beats_discovery(store):
    """agent_mcp's contract, and worth keeping: pointing Amber at a local peer during
    an incident must not be silently undone by the next refresh."""
    store.payload = {"servers": [{"name": "bloom", "base_url": "https://bloom.test"}]}
    await peers.refresh_once(_settings())

    client = brain.build_broker(
        _settings(mcp_peers="bloom=http://127.0.0.1:8010", mcp_peer_token="local")
    ).brokers[-1]

    assert client._resolve("bloom")["base_url"] == "http://127.0.0.1:8010"
    assert client._resolve("bloom")["token"] == "local"


async def test_both_layers_are_offered_not_just_one(store):
    store.payload = {"servers": [{"name": "bloom", "base_url": "https://bloom.test"}]}
    await peers.refresh_once(_settings())

    client = brain.build_broker(_settings(mcp_peers="finance=https://finance.test")).brokers[-1]

    assert client.servers == ["bloom", "finance"]


def test_the_static_layer_is_re_asserted_every_turn(store):
    """Rebuilt per turn, so a peer removed from the env map does not linger in the
    process-wide registry for the life of the process."""
    brain.build_broker(_settings(mcp_peers="finance=https://finance.test"))
    assert default_registry().known() == ["finance"]

    brain.build_broker(_settings(mcp_peers=""))
    assert default_registry().known() == []


def test_ambers_own_tools_still_come_first(store):
    """Priority order is the guarantee that a peer cannot shadow a local tool."""
    default_registry()._discovered = {
        "bloom": PeerRecord(name="bloom", base_url="https://bloom.test")
    }
    broker = brain.build_broker(_settings())

    assert isinstance(broker.brokers[0], brain.AnthropicRegistryBroker)
    assert isinstance(broker.brokers[-1], brain.MCPClient)


# --- gating -----------------------------------------------------------------


def test_discovery_needs_a_store():
    assert peers.discovery_enabled(_settings(mcp_sync_store_url="")) is False
    assert peers.discovery_enabled(_settings()) is True


def test_the_flag_pins_amber_to_the_static_map():
    """The switch for a bad registry entry sending her somewhere she should not go."""
    assert peers.discovery_enabled(_settings(feature_peer_discovery=False)) is False


def test_tools_off_disables_discovery_too():
    """No point pulling a peer list for a brain that will be offered no tools."""
    assert peers.discovery_enabled(_settings(feature_tools=False)) is False


async def test_a_disabled_pull_makes_no_request(store):
    assert await peers.refresh_once(_settings(mcp_sync_store_url="")) == 0
    assert store.calls == []


async def test_the_pull_presents_the_store_token(store):
    store.payload = {"servers": []}
    await peers.refresh_once(_settings())

    url, headers = store.calls[0]
    assert url == "https://sync.test/servers"
    assert headers["Authorization"] == "Bearer store-token"


# --- failure isolation ------------------------------------------------------


async def test_an_unreachable_store_keeps_the_peers_already_known(store):
    """Discovery is an amplifier, never a dependency. A failed pull must not empty
    the list Amber is currently serving from."""
    store.payload = {"servers": [{"name": "bloom", "base_url": "https://bloom.test"}]}
    await peers.refresh_once(_settings())

    store.error = RuntimeError("connection refused")
    assert await peers.refresh_once(_settings()) == 0

    assert default_registry().resolve("bloom").base_url == "https://bloom.test"


async def test_a_failed_pull_is_recorded_rather_than_raised(store):
    store.error = RuntimeError("connection refused")
    await peers.refresh_once(_settings())
    # PeerRegistry.refresh swallows by contract and returns 0, so nothing propagates
    # here; what matters is that the pass completes and the count is honest.
    assert peers.status(_settings())["discovered"] == 0


async def test_a_successful_pull_records_when(store):
    store.payload = {"servers": [{"name": "bloom", "base_url": "https://bloom.test"}]}
    await peers.refresh_once(_settings())

    state = peers.status(_settings())
    assert state["enabled"] is True
    assert state["discovered"] == 1
    assert state["last_ok"] is not None
    assert state["last_error"] is None


async def test_a_malformed_record_is_skipped_not_fatal(store):
    """The store is another service and may be a version ahead. One bad row must not
    cost every other peer."""
    store.payload = {
        "servers": [
            {"name": "", "base_url": "https://nameless.test"},
            {"name": "broken"},
            {"name": "bloom", "base_url": "https://bloom.test"},
        ]
    }
    assert await peers.refresh_once(_settings()) == 1
    assert default_registry().known() == ["bloom"]


# --- the loop ---------------------------------------------------------------


async def test_the_loop_pulls_immediately(store):
    """A peer connected from Aperture while this box was restarting should be
    callable on the first turn back, not five minutes into it."""
    store.payload = {"servers": [{"name": "bloom", "base_url": "https://bloom.test"}]}
    settings = _settings(peer_sync_interval_s=3600)

    task = asyncio.create_task(peers.refresh_loop(settings))
    for _ in range(50):
        await asyncio.sleep(0)
        if store.calls:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.calls, "the first pass should not wait for the interval"


async def test_the_loop_returns_at_once_when_disabled(store):
    await asyncio.wait_for(peers.refresh_loop(_settings(mcp_sync_store_url="")), timeout=1)
    assert store.calls == []
