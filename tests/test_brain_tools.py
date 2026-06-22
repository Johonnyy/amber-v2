"""Tests for the brain's tool-use loop (Anthropic client + tools faked, no network)."""

import pytest

import app.brain as brain


# --- fakes for the Anthropic streaming API ---

class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FinalMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeStream:
    """One `messages.stream(...)` context: streams chunks, then a final message."""

    def __init__(self, chunks, final):
        self._chunks = chunks
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for c in self._chunks:
                yield c

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


async def _collect(messages, system=None):
    return [t async for t in brain.think(messages, system=system)]


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
