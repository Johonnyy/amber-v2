"""Tests for turn visibility: the ``activity`` broker and the ``delta`` tee.

The point of these frames is that a client can draw what a turn is *doing*, so the
tests are mostly about the guarantees a drawing depends on: that a start always
precedes its end, that the pair shares an id, that a call is attributed to the right
broker, and — the two that actually bite — that nothing here can fail a turn and that
a barge-in stays fast.
"""

import asyncio

import pytest

from app import activity as activity_stream
from app import protocol
from app.activity import ActivityBroker
from app.peers import classify_tool_name


class Recorder:
    """A frame sink that just remembers."""

    def __init__(self):
        self.frames: list[dict] = []

    async def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    def of_type(self, kind: str) -> list[dict]:
        return [f for f in self.frames if f["type"] == kind]


class FakeBroker:
    """The inner broker: returns, raises, hangs, or reports what it was bound with."""

    def __init__(self, result="ok", *, raises=None, hang=False, schemas=None):
        self.result = result
        self.raises = raises
        self.hang = hang
        self.schemas = schemas or []
        self.calls: list[tuple[str, dict]] = []
        self.bound: dict | None = None
        self.closed = False

    async def list_tools(self):
        return self.schemas

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.hang:
            await asyncio.Event().wait()
        if self.raises is not None:
            raise self.raises
        return self.result

    def bind(self, **kwargs):
        self.bound = kwargs

    async def aclose(self):
        self.closed = True


def schema(name: str, *, read_only: bool = False) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {}},
        "x_agent": {"read_only": read_only, "requires_confirmation": False},
    }


# --- classification -------------------------------------------------------- #


@pytest.mark.parametrize(
    "name, peers, expected",
    [
        ("web_search", (), protocol.ORIGIN_OWN),
        ("client_show_text", (), protocol.ORIGIN_CLIENT),
        ("expect_reply", (), protocol.ORIGIN_SIGNAL),
        ("bloom__build_agent", ("bloom",), "peer:bloom"),
        # An unknown prefix is not a peer just because it has a separator in it.
        ("bloom__build_agent", (), protocol.ORIGIN_OWN),
        ("weird__local_tool", (), protocol.ORIGIN_OWN),
    ],
)
def test_classify_tool_name(name, peers, expected):
    assert classify_tool_name(name, peers) == expected


def test_a_peer_named_client_is_not_mistaken_for_the_device():
    """The ordering that makes `classify_tool_name` a function rather than two ifs.

    ``client__foo`` from a peer called ``client`` matches ``startswith("client_")``
    perfectly happily, so checking the device prefix first would file another agent's
    tools as this laptop's.
    """
    assert classify_tool_name("client__foo", ("client",)) == "peer:client"
    assert classify_tool_name("client_foo", ("client",)) == protocol.ORIGIN_CLIENT


def test_the_signal_tool_name_matches_the_brain():
    """`app.peers` names ``expect_reply`` rather than importing it, to dodge a cycle.

    Which means nothing but this test stops the two drifting apart.
    """
    from app.brain import EXPECT_REPLY_TOOL
    from app.peers import SIGNAL_TOOL_NAMES

    assert EXPECT_REPLY_TOOL in SIGNAL_TOOL_NAMES


# --- the broker ------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_a_call_emits_a_start_and_an_end_sharing_an_id():
    emit = Recorder()
    broker = ActivityBroker(FakeBroker("found 3 results"), emit, peers=())

    assert await broker.call_tool("web_search", {"query": "otters"}) == "found 3 results"

    start, end = emit.of_type(protocol.ACTIVITY)
    assert start["phase"] == protocol.PHASE_START
    assert end["phase"] == protocol.PHASE_END
    assert start["id"] == end["id"]
    assert start["name"] == end["name"] == "web_search"
    assert start["input"] == {"query": "otters"}
    assert end["ok"] is True
    assert end["result"] == "found 3 results"
    assert "ms" in end
    # A start says nothing about an outcome it cannot know yet.
    assert "ok" not in start


@pytest.mark.asyncio
async def test_the_result_is_passed_through_untouched():
    """Wrapping must be invisible to the model, however it looks on the wire."""
    long_result = "x" * (activity_stream.MAX_WIRE_RESULT + 500)
    emit = Recorder()
    broker = ActivityBroker(FakeBroker(long_result), emit)

    returned = await broker.call_tool("read_url", {})

    assert returned == long_result  # the model gets everything
    end = emit.of_type(protocol.ACTIVITY)[1]
    assert len(end["result"]) < len(long_result)  # the wire gets a preview
    assert "result truncated" in end["result"]


@pytest.mark.asyncio
async def test_argument_values_are_clamped_individually():
    """Per-value, so one enormous field can't push the others off a card."""
    emit = Recorder()
    broker = ActivityBroker(FakeBroker(), emit)

    await broker.call_tool(
        "bloom_create_agent",
        {"slug": "spotify-dj", "system_prompt": "y" * 5000, "steps": 12},
    )

    sent = emit.of_type(protocol.ACTIVITY)[0]["input"]
    assert sent["slug"] == "spotify-dj"  # short values survive intact
    assert sent["steps"] == 12  # and non-strings are left alone
    assert len(sent["system_prompt"]) < 5000


@pytest.mark.asyncio
async def test_an_error_result_is_reported_as_not_ok():
    emit = Recorder()
    broker = ActivityBroker(FakeBroker("Error: tool 'x' is not available."), emit)

    await broker.call_tool("x", {})

    assert emit.of_type(protocol.ACTIVITY)[1]["ok"] is False


@pytest.mark.asyncio
async def test_read_only_rides_along_from_the_schema():
    emit = Recorder()
    inner = FakeBroker(schemas=[schema("search_memory", read_only=True), schema("add_task")])
    broker = ActivityBroker(inner, emit)

    await broker.list_tools()  # the runner always does this first
    await broker.call_tool("search_memory", {})
    await broker.call_tool("add_task", {})

    starts = [f for f in emit.of_type(protocol.ACTIVITY) if f["phase"] == "start"]
    assert starts[0]["read_only"] is True
    assert starts[1]["read_only"] is False


@pytest.mark.asyncio
async def test_list_tools_survives_a_schema_it_cannot_read():
    """The runner swallows an exception here and carries on with *no tools at all*.

    So a stray KeyError in this bookkeeping would silently disarm every tool Amber
    has, and the only evidence would be a log line.
    """
    emit = Recorder()
    inner = FakeBroker(schemas=[{"nonsense": True}, None, schema("web_search")])
    broker = ActivityBroker(inner, emit)

    assert await broker.list_tools() == inner.schemas


# --- the guarantees that actually bite ------------------------------------- #


@pytest.mark.asyncio
async def test_a_dead_socket_never_fails_a_tool_call():
    """The whole posture: telemetry is not worth a turn."""

    async def broken_emit(frame):
        raise RuntimeError('Cannot call "send" once a close message has been sent.')

    broker = ActivityBroker(FakeBroker("still fine"), broken_emit)

    assert await broker.call_tool("web_search", {}) == "still fine"


@pytest.mark.asyncio
async def test_a_barge_in_emits_nothing_and_unwinds():
    """No closing frame on cancellation, and deliberately so.

    That path also runs when the *connection* is closing, so the socket may be gone;
    and it sits between the user interrupting and Amber listening again, so every
    await on it is latency. A client knows it interrupted.
    """
    emit = Recorder()
    broker = ActivityBroker(FakeBroker(hang=True), emit)

    task = asyncio.create_task(broker.call_tool("bloom__build_agent", {}))
    await asyncio.sleep(0)  # let it reach the hang
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    frames = emit.of_type(protocol.ACTIVITY)
    assert len(frames) == 1
    assert frames[0]["phase"] == protocol.PHASE_START


@pytest.mark.asyncio
async def test_an_unexpected_exception_is_reported_and_re_raised():
    """Brokers below turn errors into strings, so this is the surprising case.

    Swallowing it would hand the model a silent empty result.
    """
    emit = Recorder()
    broker = ActivityBroker(FakeBroker(raises=ValueError("boom")), emit)

    with pytest.raises(ValueError):
        await broker.call_tool("web_search", {})

    end = emit.of_type(protocol.ACTIVITY)[1]
    assert end["ok"] is False
    assert "boom" in end["result"]


@pytest.mark.asyncio
async def test_bind_is_forwarded():
    """Not part of the ToolBroker protocol — the runner reaches for it by getattr —
    which is exactly why a wrapper is easy to write without it. ``bind`` carries the
    depth that stops agent-to-agent call loops, so eating it would disable the cap.
    """
    inner = FakeBroker()
    broker = ActivityBroker(inner, Recorder())

    broker.bind(conversation_id="s-1", depth=2)

    assert inner.bound == {"conversation_id": "s-1", "depth": 2}


@pytest.mark.asyncio
async def test_bind_does_not_swallow_a_depth_refusal():
    class Refusing(FakeBroker):
        def bind(self, **kwargs):
            raise RuntimeError("agent depth 5 exceeds the limit of 5")

    broker = ActivityBroker(Refusing(), Recorder())

    with pytest.raises(RuntimeError):
        broker.bind(depth=5)


@pytest.mark.asyncio
async def test_aclose_is_forwarded():
    inner = FakeBroker()
    broker = ActivityBroker(inner, Recorder())
    await broker.aclose()
    assert inner.closed


def test_wrap_is_a_no_op_when_there_is_nothing_to_do():
    inner = FakeBroker()
    assert activity_stream.wrap(inner, Recorder(), enabled=False) is inner
    assert activity_stream.wrap(inner, None) is inner
    assert activity_stream.wrap(None, Recorder()) is None
    assert isinstance(activity_stream.wrap(inner, Recorder()), ActivityBroker)
