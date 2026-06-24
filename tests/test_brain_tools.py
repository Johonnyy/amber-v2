"""Tests for the brain's tool-use loop (Anthropic client + tools faked, no network)."""

import pytest

import app.brain as brain


# --- fakes for the Anthropic streaming API ---

class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Ev:
    """A streaming event: a ``.type`` plus whatever attributes that type carries."""

    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


def _starts(block_type):
    """A ``content_block_start`` event for a block of the given type (e.g. a tool)."""
    return _Ev("content_block_start", content_block=_Block(block_type))


class _FinalMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeStream:
    """One `messages.stream(...)` context: yields events, then a final message.

    The brain consumes the stream event by event (`async for event in stream`).
    Items in ``chunks`` are either plain strings — shorthand for a ``text`` delta
    event — or pre-built event objects (e.g. a ``content_block_start`` via
    :func:`_starts`), so a test can interleave a tool-block start with text deltas.
    """

    def __init__(self, chunks, final):
        self._chunks = chunks
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield _Ev("text", text=c) if isinstance(c, str) else c

        return gen()

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, streams):
        self._streams = list(streams)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._streams.pop(0)


class _FakeClient:
    def __init__(self, streams):
        self.messages = _FakeMessages(streams)


@pytest.fixture(autouse=True)
def _no_server_tools(monkeypatch):
    """Default Anthropic's native server-side tools off; tests opt in explicitly."""
    monkeypatch.setattr(brain, "get_server_tool_schemas", lambda: [])


async def _collect(messages, system=None, signals=None):
    return [t async for t in brain.think(messages, system=system, signals=signals)]


async def test_no_tools_path_streams_directly(monkeypatch):
    """With no tools available the brain takes the plain Phase-2/3 stream path."""
    final = _FinalMessage([_Block("text", text="Hello.")], "end_turn")
    client = _FakeClient([_FakeStream(["Hello."], final)])
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [])

    out = await _collect([{"role": "user", "content": "hi"}])
    assert "".join(out) == "Hello."
    # One call, and no tools were offered.
    assert len(client.messages.calls) == 1
    assert "tools" not in client.messages.calls[0]


async def test_tool_loop_executes_then_answers(monkeypatch):
    tool_block = _Block("tool_use", id="t1", name="web_search", input={"query": "x"})
    streams = [
        _FakeStream(
            ["Let me check. "],
            _FinalMessage(
                [_Block("text", text="Let me check. "), tool_block], "tool_use"
            ),
        ),
        _FakeStream(
            ["The answer is 42."],
            _FinalMessage([_Block("text", text="The answer is 42.")], "end_turn"),
        ),
    ]
    client = _FakeClient(streams)
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [{"name": "web_search"}])

    calls = []

    async def fake_run_tool(name, tool_input):
        calls.append((name, tool_input))
        return "result: 42"

    monkeypatch.setattr(brain, "run_tool", fake_run_tool)

    out = await _collect([{"role": "user", "content": "hi"}], system="S")
    assert "".join(out) == "Let me check. The answer is 42."
    assert calls == [("web_search", {"query": "x"})]

    # The second LLM call carried the assistant tool_use turn + the tool_result.
    second = client.messages.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    tool_result = second[-1]
    assert tool_result["role"] == "user"
    assert tool_result["content"][0]["type"] == "tool_result"
    assert tool_result["content"][0]["tool_use_id"] == "t1"
    assert tool_result["content"][0]["content"] == "result: 42"


async def test_preamble_flushes_before_server_tool(monkeypatch):
    """When the model stops speaking to run a server tool (native web search), a
    newline is emitted right then — the flush hint that lets the spoken preamble
    reach TTS *before* the search runs, instead of being bundled with the answer."""
    native = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    final = _FinalMessage(
        [
            _Block("text", text="Let me check that."),
            _Block("server_tool_use", id="s1", name="web_search", input={"query": "x"}),
            _Block("web_search_tool_result", tool_use_id="s1", content=[]),
            _Block("text", text="It's on Sunday."),
        ],
        "end_turn",
    )
    # The preamble streams, then the search block starts, then the answer streams.
    stream = _FakeStream(
        ["Let me check that.", _starts("server_tool_use"), "It's on Sunday."],
        final,
    )
    client = _FakeClient([stream])
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [])
    monkeypatch.setattr(brain, "get_server_tool_schemas", lambda: [native])

    out = await _collect([{"role": "user", "content": "when's the final?"}])
    # The "\n" lands between preamble and answer; a text-only block start gets none.
    assert out == ["Let me check that.", "\n", "It's on Sunday."]
    assert client.messages.calls[0]["tools"] == [native]


async def test_server_tool_offered_and_pause_turn_resumes(monkeypatch):
    """Native web search is a server tool: offered on the request, never dispatched.
    A ``pause_turn`` echoes the partial assistant turn back so the server resumes."""
    native = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
    streams = [
        # The server runs the search, streams some text, and pauses the turn.
        _FakeStream(
            ["Looking that up. "],
            _FinalMessage([_Block("text", text="Looking that up. ")], "pause_turn"),
        ),
        # Resumed: the model finishes the answer with what it found.
        _FakeStream(
            ["The final is on Sunday."],
            _FinalMessage([_Block("text", text="The final is on Sunday.")], "end_turn"),
        ),
    ]
    client = _FakeClient(streams)
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [])
    monkeypatch.setattr(brain, "get_server_tool_schemas", lambda: [native])

    # A tool dispatch here would be a bug — server tools run on Anthropic's side.
    async def boom(name, tool_input):
        raise AssertionError("server tools must not be dispatched locally")

    monkeypatch.setattr(brain, "run_tool", boom)

    out = await _collect([{"role": "user", "content": "when's the final?"}])
    assert "".join(out) == "Looking that up. The final is on Sunday."

    # The native tool was offered on the request...
    assert client.messages.calls[0]["tools"] == [native]
    # ...and the pause echoed the partial assistant turn straight back (no tool_result).
    assert len(client.messages.calls) == 2
    resumed = client.messages.calls[1]["messages"]
    assert resumed[-1]["role"] == "assistant"
    assert resumed[-1]["content"][0].text == "Looking that up. "


async def test_caller_history_not_mutated(monkeypatch):
    tool_block = _Block("tool_use", id="t1", name="web_search", input={})
    streams = [
        _FakeStream([], _FinalMessage([tool_block], "tool_use")),
        _FakeStream(["done"], _FinalMessage([_Block("text", text="done")], "end_turn")),
    ]
    client = _FakeClient(streams)
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [{"name": "web_search"}])

    async def fake_run_tool(name, tool_input):
        return "ok"

    monkeypatch.setattr(brain, "run_tool", fake_run_tool)

    history = [{"role": "user", "content": "hi"}]
    await _collect(history)
    # The brain works on a copy — the caller's history is untouched.
    assert history == [{"role": "user", "content": "hi"}]


# --- turn-based conversations: the expect_reply signaling tool ---


async def test_expect_reply_sets_signal_and_still_speaks(monkeypatch):
    """Calling expect_reply flips the turn signal, returns an ack to the loop, and
    the spoken question still streams through — never routed to the registry."""
    from app.turn_signals import TurnSignals

    reply_call = _Block("tool_use", id="e1", name=brain.EXPECT_REPLY_TOOL, input={})
    streams = [
        _FakeStream(
            ["Which one do you mean? "],
            _FinalMessage(
                [_Block("text", text="Which one do you mean? "), reply_call],
                "tool_use",
            ),
        ),
        # After the ack is fed back the model finishes (no more text).
        _FakeStream([], _FinalMessage([], "end_turn")),
    ]
    client = _FakeClient(streams)
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [])

    async def boom(name, tool_input):
        raise AssertionError("expect_reply must not be dispatched to the registry")

    monkeypatch.setattr(brain, "run_tool", boom)

    signals = TurnSignals()
    out = await _collect([{"role": "user", "content": "play it"}], signals=signals)

    assert "".join(out) == "Which one do you mean? "
    assert signals.awaiting_response is True
    # The tool was advertised on the first request...
    assert any(
        t.get("name") == brain.EXPECT_REPLY_TOOL
        for t in client.messages.calls[0]["tools"]
    )
    # ...and the ack came back as the tool_result for the next iteration.
    tool_result = client.messages.calls[1]["messages"][-1]
    assert tool_result["content"][0]["tool_use_id"] == "e1"
    assert "wait" in tool_result["content"][0]["content"].lower()


async def test_expect_reply_not_offered_when_feature_off(monkeypatch):
    from app.turn_signals import TurnSignals

    monkeypatch.setenv("AMBER_FEATURE_TURN_BASED", "false")
    brain.get_settings.cache_clear()
    final = _FinalMessage([_Block("text", text="Sure.")], "end_turn")
    client = _FakeClient([_FakeStream(["Sure."], final)])
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [])

    try:
        signals = TurnSignals()
        out = await _collect([{"role": "user", "content": "hi"}], signals=signals)
        assert "".join(out) == "Sure."
        assert signals.awaiting_response is False
        # No tools at all -> the no-tools fast path (no `tools` kwarg on the call).
        assert "tools" not in client.messages.calls[0]
    finally:
        brain.get_settings.cache_clear()


async def test_expect_reply_offered_but_not_called_leaves_flag_false(monkeypatch):
    """Feature on + signals passed -> tool is offered, but an ordinary turn that
    never calls it leaves awaiting_response False (most turns just end)."""
    from app.turn_signals import TurnSignals

    final = _FinalMessage([_Block("text", text="It's sunny.")], "end_turn")
    client = _FakeClient([_FakeStream(["It's sunny."], final)])
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [])

    signals = TurnSignals()
    out = await _collect([{"role": "user", "content": "weather?"}], signals=signals)
    assert "".join(out) == "It's sunny."
    assert signals.awaiting_response is False
    assert any(
        t.get("name") == brain.EXPECT_REPLY_TOOL
        for t in client.messages.calls[0]["tools"]
    )


async def test_iteration_cap_forces_final_answer(monkeypatch):
    monkeypatch.setenv("AMBER_MAX_TOOL_ITERATIONS", "1")
    brain.get_settings.cache_clear()

    tool_block = _Block("tool_use", id="t1", name="web_search", input={})
    streams = [
        # The single allowed tool iteration keeps asking for a tool...
        _FakeStream([], _FinalMessage([tool_block], "tool_use")),
        # ...so the safety valve fires a final, tools-off stream for the answer.
        _FakeStream(["Final answer."], _FinalMessage([], "end_turn")),
    ]
    client = _FakeClient(streams)
    monkeypatch.setattr(brain, "get_client", lambda: client)
    monkeypatch.setattr(brain, "get_tool_schemas", lambda: [{"name": "web_search"}])

    async def fake_run_tool(name, tool_input):
        return "ok"

    monkeypatch.setattr(brain, "run_tool", fake_run_tool)

    try:
        out = await _collect([{"role": "user", "content": "hi"}])
        assert "".join(out) == "Final answer."
        # Final call is the tools-off safety valve.
        assert "tools" not in client.messages.calls[-1]
    finally:
        brain.get_settings.cache_clear()
