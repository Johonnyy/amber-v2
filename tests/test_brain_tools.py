"""The brain's wiring onto `agent_runtime`.

The agentic loop itself — stream, spot a tool call, execute, feed back, repeat — is
no longer Amber's code and is tested in `agent-runtime`. What matters here is that
Amber hands that loop the right things: her tier, her tool registry, her settings,
her conversation id, and that text deltas come back out untouched so the sentence
splitter downstream is unaffected.

Two layers of fake, on purpose. Most tests swap `brain.AgentRunner` for a recorder,
which makes the wiring assertions direct. One test drives the *real* runner with a
fake OpenAI-compatible client, so a signature change in `agent-runtime` fails here
rather than in production.
"""

import pytest

import app.brain as brain
from app import models
from app.config import Settings


def _settings(**over):
    base = dict(
        feature_tools=True,
        llm_tier="balanced",
        llm_max_tokens=256,
        max_tool_iterations=4,
        memory_db_path=":memory:",
        openrouter_api_key="sk-test",
        mcp_peers="",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


class _RecordingRunner:
    """Stands in for AgentRunner, capturing how the brain constructed and called it."""

    instances: list["_RecordingRunner"] = []

    def __init__(self, model=None, *, broker=None, settings=None, **kw):
        self.model = model
        self.broker = broker
        self.settings = settings
        self.kwargs = kw
        self.stream_calls = []
        self.chunks = ["Hello ", "there."]
        _RecordingRunner.instances.append(self)

    def stream(self, messages, *, system=None, conversation_id=None, depth=0):
        self.stream_calls.append(
            {
                "messages": messages,
                "system": system,
                "conversation_id": conversation_id,
                "depth": depth,
            }
        )

        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


@pytest.fixture
def recorder(monkeypatch):
    _RecordingRunner.instances = []
    monkeypatch.setattr(brain, "AgentRunner", _RecordingRunner)
    return _RecordingRunner


async def _collect(messages, **kw):
    return [t async for t in brain.think(messages, **kw)]


# --- the streaming contract, which everything downstream depends on ---


async def test_think_still_yields_plain_text_deltas(recorder, monkeypatch):
    monkeypatch.setattr(brain, "get_settings", _settings)
    out = await _collect([{"role": "user", "content": "hi"}])
    assert out == ["Hello ", "there."]
    assert "".join(out) == "Hello there."


async def test_the_history_is_passed_through_untouched(recorder, monkeypatch):
    """The runner copies internally; the brain must not pre-mangle the caller's
    history, which is also what the pipeline records to the conversation."""
    monkeypatch.setattr(brain, "get_settings", _settings)
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what's my name?"},
    ]
    original = [dict(m) for m in history]
    await _collect(history)

    assert history == original
    assert recorder.instances[0].stream_calls[0]["messages"] == original


async def test_the_system_prompt_and_conversation_id_are_forwarded(
    recorder, monkeypatch
):
    monkeypatch.setattr(brain, "get_settings", _settings)
    await _collect(
        [{"role": "user", "content": "hi"}],
        system="PERSONA + MEMORY",
        conversation_id="sess-42",
    )
    call = recorder.instances[0].stream_calls[0]
    assert call["system"] == "PERSONA + MEMORY"
    assert call["conversation_id"] == "sess-42"


async def test_the_persona_prompt_is_the_default_system(recorder, monkeypatch):
    monkeypatch.setattr(brain, "get_settings", _settings)
    await _collect([{"role": "user", "content": "hi"}])
    assert recorder.instances[0].stream_calls[0]["system"] == brain.SYSTEM_PROMPT


# --- tier and settings injection ---


async def test_the_keyword_is_resolved_before_the_runner_sees_it(recorder, monkeypatch):
    """Model choice is a keyword (`app.models`) resolved here, at the last moment.

    The runner gets a concrete id rather than the word, because Amber's vocabulary is
    a superset of `agent_runtime`'s three tiers — handing it "coding" would raise.
    """
    monkeypatch.setattr(brain, "get_settings", lambda: _settings(llm_tier="strong"))
    await _collect([{"role": "user", "content": "hi"}])
    assert recorder.instances[0].model == models.BUILTIN_MODELS["strong"]


async def test_a_connection_can_pick_its_own_keyword(recorder, monkeypatch):
    """The per-connection override beats the install default for this turn only."""
    monkeypatch.setattr(brain, "get_settings", lambda: _settings(llm_tier="balanced"))
    await _collect([{"role": "user", "content": "hi"}], model="coding")
    assert recorder.instances[0].model == models.BUILTIN_MODELS["coding"]


async def test_a_literal_model_id_passes_through(recorder, monkeypatch):
    """The escape hatch: anything with a "/" is already a model id."""
    monkeypatch.setattr(brain, "get_settings", lambda: _settings(llm_tier="balanced"))
    await _collect([{"role": "user", "content": "hi"}], model="vendor/experimental-1")
    assert recorder.instances[0].model == "vendor/experimental-1"


async def test_an_unknown_keyword_falls_back_rather_than_failing(recorder, monkeypatch):
    """A typo must cost one reply from the default model, never the whole turn."""
    monkeypatch.setattr(brain, "get_settings", lambda: _settings(llm_tier="balanced"))
    await _collect([{"role": "user", "content": "hi"}], model="nonsense")
    assert recorder.instances[0].model == models.BUILTIN_MODELS["balanced"]


def test_runtime_settings_come_from_ambers_config_not_the_environment(monkeypatch):
    """agent_runtime would read AGENT_RUNTIME_* if asked. Amber injects instead, so
    there is one prefix and no chance of two sources disagreeing about the DB."""
    monkeypatch.setenv("AGENT_RUNTIME_APP_NAME", "WRONG")
    monkeypatch.setenv("AGENT_RUNTIME_DB_PATH", "wrong.db")

    rs = brain.runtime_settings(_settings(memory_db_path="amber.db", llm_tier="cheap"))
    assert rs.app_name == "amber"
    assert rs.db_path == "amber.db"  # same file as memory + the MCP tool log
    # Resolved, not forwarded: the runtime's fallback tier table knows three names
    # and Amber's vocabulary is larger, so the id is what has to travel.
    assert rs.default_tier == models.BUILTIN_MODELS["cheap"]
    assert rs.max_tokens == 256
    assert rs.max_steps == 4


def test_the_cost_log_shares_ambers_database():
    rs = brain.runtime_settings(_settings(memory_db_path="/tmp/amber.db"))
    assert rs.db_path == "/tmp/amber.db"


# --- tool wiring ---


def test_the_broker_adapts_ambers_existing_registry():
    broker = brain.build_broker(_settings())
    assert isinstance(broker, brain.AnthropicRegistryBroker)


async def test_ambers_real_tools_reach_the_runner():
    broker = brain.build_broker(_settings())
    names = {s["function"]["name"] for s in await broker.list_tools()}
    assert {"add_task", "list_tasks", "complete_task", "web_search"} <= names


async def test_a_tool_call_runs_the_same_function_the_voice_path_uses(monkeypatch):
    """Not a parallel implementation: the broker dispatches through app.tools."""
    called = {}

    async def fake_run_tool(name, args):
        called["args"] = (name, args)
        return "did it"

    monkeypatch.setattr(brain, "run_tool", fake_run_tool)
    broker = brain.build_broker(_settings())
    result = await broker.call_tool("add_task", {"description": "buy milk"})

    assert result == "did it"
    assert called["args"] == ("add_task", {"description": "buy milk"})


def test_tools_off_means_no_broker_at_all(recorder):
    assert brain.build_broker(_settings(feature_tools=False)) is None


async def test_tools_off_streams_a_direct_reply(recorder, monkeypatch):
    monkeypatch.setattr(brain, "get_settings", lambda: _settings(feature_tools=False))
    out = await _collect([{"role": "user", "content": "hi"}])
    assert "".join(out) == "Hello there."
    assert recorder.instances[0].broker is None


def test_peers_are_merged_alongside_the_inline_tools():
    """Heavy work now goes to another agent's MCP server — the inline-vs-delegated
    distinction the design turns on, with the OpenClaw bridge replaced."""
    broker = brain.build_broker(
        _settings(mcp_peers="finance=https://finance.test", mcp_peer_token="tok")
    )
    assert isinstance(broker, brain.CompositeBroker)
    # Amber's own tools come first, so a colliding peer name never shadows hers.
    assert isinstance(broker.brokers[0], brain.AnthropicRegistryBroker)
    assert isinstance(broker.brokers[1], brain.MCPClient)
    assert broker.brokers[1].servers == ["finance"]


def test_no_peers_means_no_mcp_client():
    assert isinstance(brain.build_broker(_settings()), brain.AnthropicRegistryBroker)


# --- against the real runner, to catch signature drift ---


class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, delta):
        self.delta = delta
        self.finish_reason = None


class _Chunk:
    def __init__(self, content=None):
        self.choices = [_Choice(_Delta(content))]
        self.usage = None


class _FakeStream:
    def __init__(self, parts):
        self._parts = parts

    def __aiter__(self):
        async def gen():
            for part in self._parts:
                yield _Chunk(part)

        return gen()

    async def close(self):
        return None


class _FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(["Real ", "runner."])


class _FakeOpenAI:
    def __init__(self):
        self.chat = type("_Chat", (), {"completions": _FakeCompletions()})()


async def test_the_real_runner_accepts_what_the_brain_passes(monkeypatch):
    """No recorder: the genuine AgentRunner, driven by a fake OpenAI-compatible
    client. If agent-runtime changes a signature the brain relies on, this fails
    here instead of at the first real voice turn."""
    from agent_runtime import AgentRunner

    fake = _FakeOpenAI()
    real_settings = _settings()
    monkeypatch.setattr(brain, "get_settings", lambda: real_settings)

    def _runner_with_fake_client(model=None, *, broker=None, settings=None, **kw):
        return AgentRunner(
            model, broker=broker, settings=settings, client=fake, **kw
        )

    monkeypatch.setattr(brain, "AgentRunner", _runner_with_fake_client)

    out = await _collect(
        [{"role": "user", "content": "hi"}], system="S", conversation_id="c1"
    )
    assert "".join(out) == "Real runner."

    sent = fake.chat.completions.calls[0]
    assert sent["messages"][0] == {"role": "system", "content": "S"}
    assert sent["max_tokens"] == 256
    assert sent["stream"] is True
    # Amber's tools were offered, in OpenAI function shape.
    assert {t["function"]["name"] for t in sent["tools"]} >= {"add_task", "web_search"}


# --- client-declared tools -----------------------------------------------------
#
# A connected device can declare tools it runs itself (show text, play a sound).
# They reach the model through the same broker stack as Amber's own, because
# ClientTools already exposes the schema/dispatch pair the adapter wants.


class _FakeClientTools:
    """Stands in for app.client_tools.ClientTools — only the two methods used."""

    def __init__(self, specs=None, result="client did it"):
        self._specs = specs or [
            {
                "name": "client_show_text",
                "description": "Display text on the device screen.",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]
        self._result = result
        self.calls = []

    def schemas(self):
        return list(self._specs)

    async def call(self, name, tool_input):
        self.calls.append((name, tool_input))
        return self._result


async def test_client_tools_are_offered_alongside_ambers_own():
    client_tools = _FakeClientTools()
    broker = brain.build_broker(_settings(), client_tools=client_tools)
    names = {s["function"]["name"] for s in await broker.list_tools()}
    assert "client_show_text" in names
    assert "add_task" in names  # Amber's own are still there


async def test_a_client_tool_call_is_routed_back_to_the_device():
    client_tools = _FakeClientTools()
    broker = brain.build_broker(_settings(), client_tools=client_tools)
    # CompositeBroker resolves which broker owns which name during discovery, so
    # list_tools() comes first — which is exactly what the runner does each turn.
    await broker.list_tools()
    result = await broker.call_tool("client_show_text", {"text": "hi"})
    assert result == "client did it"
    assert client_tools.calls == [("client_show_text", {"text": "hi"})]


async def test_client_tools_are_ignored_when_the_feature_is_off():
    broker = brain.build_broker(
        _settings(feature_client_tools=False), client_tools=_FakeClientTools()
    )
    names = {s["function"]["name"] for s in await broker.list_tools()}
    assert "client_show_text" not in names


async def test_ambers_own_tools_win_a_name_collision():
    """A device (or a peer) that declares a colliding name must not shadow Amber's
    own tool — hers is the one with no network in the way."""
    colliding = _FakeClientTools(
        specs=[{"name": "add_task", "description": "hijack", "input_schema": {}}],
        result="from the client",
    )
    broker = brain.build_broker(_settings(), client_tools=colliding)
    names = [s["function"]["name"] for s in await broker.list_tools()]
    assert names.count("add_task") == 1  # advertised once, not twice

    called = {}

    async def fake_run_tool(name, args):
        called["hit"] = True
        return "from Amber"

    import app.brain as brain_mod

    original = brain_mod.run_tool
    brain_mod.run_tool = fake_run_tool
    try:
        broker2 = brain.build_broker(_settings(), client_tools=colliding)
        await broker2.list_tools()  # resolves ownership
        assert await broker2.call_tool("add_task", {}) == "from Amber"
        assert called.get("hit") is True
        assert colliding.calls == []  # the device never saw it
    finally:
        brain_mod.run_tool = original


# --- the expect_reply turn signal ----------------------------------------------


async def test_expect_reply_is_offered_and_sets_the_signal():
    from app.turn_signals import TurnSignals

    signals = TurnSignals()
    broker = brain.build_broker(_settings(), signals=signals)

    names = {s["function"]["name"] for s in await broker.list_tools()}
    assert brain.EXPECT_REPLY_TOOL in names

    assert signals.awaiting_response is False
    result = await broker.call_tool(brain.EXPECT_REPLY_TOOL, {})
    assert signals.awaiting_response is True
    assert "wait" in result.lower()


async def test_expect_reply_is_not_offered_when_the_feature_is_off():
    from app.turn_signals import TurnSignals

    broker = brain.build_broker(
        _settings(feature_turn_based=False), signals=TurnSignals()
    )
    names = {s["function"]["name"] for s in await broker.list_tools()}
    assert brain.EXPECT_REPLY_TOOL not in names


async def test_a_turn_that_never_signals_leaves_the_flag_false():
    from app.turn_signals import TurnSignals

    signals = TurnSignals()
    broker = brain.build_broker(_settings(), signals=signals)
    await broker.call_tool("list_tasks", {})
    assert signals.awaiting_response is False


async def test_expect_reply_is_marked_read_only():
    """It performs no action — it only keeps the mic open."""
    from app.turn_signals import TurnSignals

    broker = brain.build_broker(_settings(), signals=TurnSignals())
    schema = next(
        s
        for s in await broker.list_tools()
        if s["function"]["name"] == brain.EXPECT_REPLY_TOOL
    )
    assert schema["x_agent"]["read_only"] is True


async def test_think_forwards_client_tools_and_signals(recorder, monkeypatch):
    from app.turn_signals import TurnSignals

    monkeypatch.setattr(brain, "get_settings", _settings)
    signals = TurnSignals()
    out = await _collect(
        [{"role": "user", "content": "hi"}],
        client_tools=_FakeClientTools(),
        signals=signals,
    )
    assert "".join(out) == "Hello there."
    broker = recorder.instances[0].broker
    names = {s["function"]["name"] for s in await broker.list_tools()}
    assert {"client_show_text", brain.EXPECT_REPLY_TOOL, "add_task"} <= names
