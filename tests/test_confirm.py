"""Tests for the confirmation pair — the source `X-Confirmed` never had.

Two things here are load-bearing beyond the happy path. It **fails closed**: every way
of not getting an approval (no client, no answer, an explicit no) refuses the call.
And `_call_confirmed` **restores the whole bind**, because `MCPClient.bind` assigns
conversation id and depth alongside the confirmed flag — so setting one naively resets
the other two, silently detaching the call from its conversation and zeroing the depth
guard that stops agent-to-agent loops.
"""

import asyncio

import pytest

import app.config as config_module
import app.confirm as confirm_module
from app import protocol
from app.confirm import ConfirmBroker, Confirmations, wrap


def _schema(name: str, *, gated: bool = False, read_only: bool = False) -> dict:
    flags = {}
    if gated:
        flags["requires_confirmation"] = True
    if read_only:
        flags["read_only"] = True
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {}},
        "x_agent": flags,
    }


class FakeBroker:
    """The broker being wrapped. Records what it was bound with and what it ran."""

    def __init__(self, schemas=None, result="done"):
        self._schemas = schemas or []
        self.calls: list[tuple[str, dict]] = []
        self.bound: dict = {}
        # What the bind looked like at the moment each call was made — this is what
        # proves the confirmed flag was set for the call and not merely at some point.
        self.bound_during: list[dict] = []
        self.result = result

    def bind(self, **kwargs):
        self.bound = dict(kwargs)

    async def list_tools(self):
        return list(self._schemas)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        self.bound_during.append(dict(self.bound))
        return self.result


class Client:
    """A connected human, who answers however the test says."""

    def __init__(self, answer=True, silent=False):
        self.frames: list[dict] = []
        self._answer = answer
        self._silent = silent
        self.confirmations: Confirmations | None = None

    async def send(self, frame: dict) -> None:
        self.frames.append(frame)
        if self._silent:
            return
        # Answer on the next tick, the way a real client's reply arrives on the
        # receive loop rather than inline.
        asyncio.get_running_loop().call_soon(
            self.confirmations.resolve, frame["id"], self._answer
        )


def _bind(client: Client) -> Confirmations:
    confirmations = Confirmations()
    confirmations.bind(client.send)
    client.confirmations = confirmations
    return confirmations


@pytest.fixture(autouse=True)
def quick_timeout(monkeypatch):
    """Keep the fail-closed timeout tests fast."""
    monkeypatch.setenv("AMBER_CONFIRM_TIMEOUT_S", "0.1")
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


# --- gating -----------------------------------------------------------------


async def test_an_ungated_tool_runs_untouched():
    inner = FakeBroker([_schema("web_search")])
    broker = ConfirmBroker(inner, Confirmations())
    await broker.list_tools()

    assert await broker.call_tool("web_search", {"q": "x"}) == "done"
    assert inner.calls == [("web_search", {"q": "x"})]


async def test_a_gated_tool_asks_and_runs_when_approved():
    inner = FakeBroker([_schema("update_server", gated=True)])
    client = Client(answer=True)
    broker = ConfirmBroker(inner, _bind(client))
    await broker.list_tools()

    assert await broker.call_tool("update_server", {}) == "done"

    assert len(client.frames) == 1
    frame = client.frames[0]
    assert frame["type"] == protocol.CONFIRM_REQUEST
    assert frame["name"] == "update_server"
    assert frame["origin"] == protocol.ORIGIN_OWN
    assert inner.calls == [("update_server", {})]


async def test_a_denied_tool_does_not_run():
    inner = FakeBroker([_schema("update_server", gated=True)])
    client = Client(answer=False)
    broker = ConfirmBroker(inner, _bind(client))
    await broker.list_tools()

    result = await broker.call_tool("update_server", {})

    assert "not approved" in result
    assert "Don't try again" in result  # a settled no, distinct from silence
    assert inner.calls == []  # the whole point


async def test_silence_is_not_approval():
    """A timeout must read as no. The turn is blocked on a person who isn't there."""
    inner = FakeBroker([_schema("update_server", gated=True)])
    client = Client(silent=True)
    broker = ConfirmBroker(inner, _bind(client))
    await broker.list_tools()

    result = await broker.call_tool("update_server", {})

    # Reported as silence, not as a refusal — the model should ask again rather than
    # treat it as a decision the user made.
    assert "in time" in result
    assert "still want it done" in result
    assert inner.calls == []


async def test_a_gated_tool_is_refused_when_nobody_is_connected():
    inner = FakeBroker([_schema("update_server", gated=True)])
    broker = ConfirmBroker(inner, Confirmations())  # never bound
    await broker.list_tools()

    result = await broker.call_tool("update_server", {})

    assert "nobody is connected" in result
    assert inner.calls == []


async def test_a_peer_tool_is_gated_by_its_own_declaration():
    """`x_agent.requires_confirmation` is read the same way for a peer's tool as for
    Amber's own — the model sees one flat list, and this is where that distinction
    survives."""
    inner = FakeBroker([_schema("bloom__run_task", gated=True)])
    client = Client(answer=True)
    broker = ConfirmBroker(inner, _bind(client), peers=("bloom",))
    await broker.list_tools()

    await broker.call_tool("bloom__run_task", {"task": "build"})

    assert client.frames[0]["origin"] == "peer:bloom"


# --- the bind trap ----------------------------------------------------------


async def test_an_approved_peer_call_carries_the_confirmed_flag():
    inner = FakeBroker([_schema("bloom__run_task", gated=True)])
    client = Client(answer=True)
    broker = ConfirmBroker(inner, _bind(client), peers=("bloom",))
    broker.bind(conversation_id="sess-1", depth=0)
    await broker.list_tools()

    await broker.call_tool("bloom__run_task", {})

    assert inner.bound_during[0]["confirmed"] is True


async def test_the_confirmed_flag_does_not_clobber_the_conversation_or_depth():
    """`MCPClient.bind` sets all three together, so passing ``confirmed`` alone would
    reset the conversation id to None and the depth guard to zero."""
    inner = FakeBroker([_schema("bloom__run_task", gated=True)])
    client = Client(answer=True)
    broker = ConfirmBroker(inner, _bind(client), peers=("bloom",))
    broker.bind(conversation_id="sess-1", depth=2)
    await broker.list_tools()

    await broker.call_tool("bloom__run_task", {})

    during = inner.bound_during[0]
    assert during["conversation_id"] == "sess-1"
    assert during["depth"] == 2


async def test_the_confirmed_flag_is_cleared_after_the_call():
    """Run-scoped upstream, so an approval must not leak to the next tool call."""
    inner = FakeBroker(
        [_schema("bloom__run_task", gated=True), _schema("bloom__list", read_only=True)]
    )
    client = Client(answer=True)
    broker = ConfirmBroker(inner, _bind(client), peers=("bloom",))
    broker.bind(conversation_id="sess-1", depth=0)
    await broker.list_tools()

    await broker.call_tool("bloom__run_task", {})
    await broker.call_tool("bloom__list", {})

    assert inner.bound_during[0]["confirmed"] is True
    assert inner.bound_during[1].get("confirmed", False) is False
    assert inner.bound["conversation_id"] == "sess-1"


# --- lifecycle --------------------------------------------------------------


async def test_unbinding_refuses_everything_still_waiting():
    """A disconnect has a correct answer — no — which the model can act on, where a
    cancellation is something it cannot describe."""
    confirmations = Confirmations()
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    confirmations.bind(send)
    pending = asyncio.create_task(
        confirmations.request("update_server", {}, origin=protocol.ORIGIN_OWN)
    )
    await asyncio.sleep(0)
    confirmations.unbind()

    assert await pending == confirm_module.DENIED


async def test_a_stale_connection_cannot_disarm_the_one_that_replaced_it():
    """`Confirmations` lives on the `Session`, which outlives its socket. If a resumed
    session let the old handler's `finally` unbind, every gated tool would refuse
    itself with "nobody is connected" while someone sat looking at the app."""
    confirmations = Confirmations()
    old, new = object(), object()
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    confirmations.bind(send, token=old)
    confirmations.bind(send, token=new)
    confirmations.unbind(token=old)

    assert confirmations.connected() is True


async def test_a_connection_still_unbinds_itself():
    confirmations = Confirmations()
    mine = object()

    async def send(frame):
        pass

    confirmations.bind(send, token=mine)
    confirmations.unbind(token=mine)
    assert confirmations.connected() is False


async def test_aclose_is_passed_down_the_chain():
    """`ActivityBroker.aclose` sits outside this and `MCPClient.aclose` inside, so a
    wrapper that doesn't forward strands a peer's MCP session."""
    closed = []

    class Closable(FakeBroker):
        async def aclose(self):
            closed.append(True)

    broker = ConfirmBroker(Closable(), Confirmations())
    await broker.aclose()

    assert closed == [True]


async def test_aclose_is_safe_on_a_broker_that_has_none():
    await ConfirmBroker(FakeBroker(), Confirmations()).aclose()


async def test_a_late_or_unknown_answer_is_dropped():
    confirmations = Confirmations()
    confirmations.resolve("nope", True)  # must not raise
    confirmations.resolve(None, True)
    confirmations.resolve(12, True)


# --- the wrapper ------------------------------------------------------------


def test_wrap_is_a_no_op_when_there_is_nothing_to_do():
    inner = FakeBroker()
    confirmations = Confirmations()

    assert wrap(None, confirmations) is None
    assert wrap(inner, None) is inner
    assert wrap(inner, confirmations, enabled=False) is inner
    assert isinstance(wrap(inner, confirmations), ConfirmBroker)


async def test_a_broken_schema_never_disarms_the_tool_list():
    """The runner swallows exceptions from `list_tools` and carries on with no tools
    at all, so a stray KeyError here would silently disarm everything Amber has."""
    inner = FakeBroker([{"nonsense": True}, _schema("web_search")])
    broker = ConfirmBroker(inner, Confirmations())

    schemas = await broker.list_tools()

    assert len(schemas) == 2
