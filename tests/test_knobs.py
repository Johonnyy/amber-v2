"""Amber changing her own settings when asked — design principle 3.

Two things here carry the design. First, that a change lands on the **next** turn and
never the current one: the whole approach rests on `run_turn` having already read
`session.voice` into a local, and if that ever stops being true a reply would change
voice halfway through. Second, the asymmetry — slowing her down is free, re-pointing a
keyword for the whole ecosystem is not, and the gate is what keeps a passing remark from
doing the second.
"""

import pytest

import app.brain as brain
import app.config as config_module
from app import protocol
from app.confirm import Confirmations
from app.knobs import Knobs
from app.memory.store import MemoryStore
from app.voice import VoiceSettings


class FakeSession:
    """Just the two attributes `Knobs` touches. It is duck-typed on purpose."""

    def __init__(self, voice: VoiceSettings | None = None, keyword=None):
        self.voice = voice or VoiceSettings()
        self.model_keyword = keyword


def _settings(**over):
    base = {
        "_env_file": None,
        "feature_self_settings": True,
        "feature_voice_control": True,
        "feature_model_control": True,
        "feature_tools": False,  # keep the registry out of these broker assertions
        "feature_turn_based": False,
        "feature_confirmations": True,
    }
    return config_module.Settings(**{**base, **over})


@pytest.fixture
def store(monkeypatch):
    """A throwaway keyword table, so a remap in one test can't leak into another.

    Patched on `app.memory`, not `app.models` — the catalogue imports `get_store`
    inside each method to avoid an import cycle, so the name it resolves is the one
    on the package.
    """
    import app.memory as memory_module
    import app.models as models_module

    s = MemoryStore(":memory:")
    monkeypatch.setattr(memory_module, "get_store", lambda: s)
    models_module.invalidate_cache()
    yield s
    models_module.invalidate_cache()
    s.close()


async def _tools(broker) -> dict:
    return {s["function"]["name"]: s for s in await broker.list_tools()}


# --- voice ------------------------------------------------------------------


async def test_set_voice_changes_the_speed():
    session = FakeSession(VoiceSettings(speed=1.15))
    out = await Knobs(session).set_voice(speed=0.9)

    assert session.voice.speed == 0.9
    assert "0.9" in out


async def test_set_voice_clamps_through_the_same_validation_the_frame_uses():
    """No parallel validation: `VoiceSettings.patched` is the one place, so a value
    asked for out loud and one sent as a frame are clamped identically."""
    session = FakeSession()
    await Knobs(session).set_voice(speed=99)
    assert session.voice.speed == 4.0

    await Knobs(session).set_voice(speed=0.01)
    assert session.voice.speed == 0.25


async def test_set_voice_leaves_absent_fields_alone():
    session = FakeSession(VoiceSettings(voice="nova", speed=1.15))
    await Knobs(session).set_voice(speed=0.9)

    assert session.voice.voice == "nova"  # untouched
    assert session.voice.speed == 0.9


async def test_set_voice_says_so_when_nothing_took():
    """Silence here would have the model confirm a change that did not happen."""
    session = FakeSession()
    out = await Knobs(session).set_voice(voice="mcconaughey")

    assert session.voice == VoiceSettings()
    assert "didn't change anything" in out


async def test_an_unknown_voice_does_not_undo_a_good_speed():
    session = FakeSession()
    await Knobs(session).set_voice(speed=0.8, voice="not-a-voice")
    assert session.voice.speed == 0.8


# --- which brain ------------------------------------------------------------


async def test_set_brain_picks_a_keyword_for_this_connection(store):
    session = FakeSession()
    out = await Knobs(session).set_brain("strong")

    assert session.model_keyword == "strong"
    assert "strong" in out


async def test_set_brain_with_nothing_resets_to_the_install_default(store):
    session = FakeSession(keyword="strong")
    out = await Knobs(session).set_brain(None)

    assert session.model_keyword is None
    assert "default" in out.lower()


async def test_set_brain_refuses_a_keyword_that_does_not_exist(store):
    session = FakeSession()
    out = await Knobs(session).set_brain("turbo")

    assert session.model_keyword is None
    assert "don't have" in out or "recognise" in out
    # And it says what it does know, so the model can pick again in the same turn.
    assert "balanced" in out


# --- remapping (install-wide) -----------------------------------------------


async def test_remap_keyword_persists_and_is_install_wide(store, monkeypatch):
    import app.model_sync as model_sync

    monkeypatch.setattr(model_sync, "schedule", lambda *a, **kw: None)
    out = await Knobs(FakeSession()).remap_keyword("coding", "vendor/some-model")

    assert "vendor/some-model" in out
    assert store.model_keywords()  # written, not just held in memory


async def test_remap_keyword_refuses_something_that_is_not_a_model_id(store):
    out = await Knobs(FakeSession()).remap_keyword("coding", "sonnet")
    assert "vendor/model" in out
    assert not store.model_keywords()


# --- the broker, and the posture --------------------------------------------


async def test_the_three_tools_are_offered():
    broker = brain.build_broker(_settings(), knobs=Knobs(FakeSession()))
    names = await _tools(broker)

    assert set(names) == {"set_voice", "set_brain", "remap_keyword"}


async def test_only_the_remap_needs_approval():
    """The design point. Slowing Amber down is per-connection and undone by saying so
    again; re-pointing a keyword is persisted and shared with every app in the
    ecosystem, so it must be harder for a passing remark to trigger."""
    broker = brain.build_broker(_settings(), knobs=Knobs(FakeSession()))
    names = await _tools(broker)

    assert names["remap_keyword"]["x_agent"]["requires_confirmation"] is True
    assert not names["set_voice"]["x_agent"].get("requires_confirmation")
    assert not names["set_brain"]["x_agent"].get("requires_confirmation")


async def test_a_remap_is_refused_when_nobody_can_approve_it(store):
    """End to end through the real gate: no client bound means no approval, and
    failing closed means the keyword table is untouched."""
    broker = brain.build_broker(
        _settings(), knobs=Knobs(FakeSession()), confirmations=Confirmations()
    )
    await broker.list_tools()

    out = await broker.call_tool(
        "remap_keyword", {"keyword": "coding", "model": "vendor/x"}
    )

    assert "nobody is connected" in out
    assert not store.model_keywords()


async def test_the_voice_tool_is_not_gated_by_the_same_broker(store):
    session = FakeSession()
    broker = brain.build_broker(
        _settings(), knobs=Knobs(session), confirmations=Confirmations()
    )
    await broker.list_tools()

    await broker.call_tool("set_voice", {"speed": 0.8})

    assert session.voice.speed == 0.8  # ran without anyone approving anything


# --- feature flags ----------------------------------------------------------


async def test_no_tools_when_self_settings_is_off():
    broker = brain.build_broker(
        _settings(feature_self_settings=False), knobs=Knobs(FakeSession())
    )
    assert broker is None


async def test_a_pinned_voice_offers_no_voice_tool():
    """Better to offer nothing than a tool that silently snaps back."""
    broker = brain.build_broker(
        _settings(feature_voice_control=False), knobs=Knobs(FakeSession())
    )
    names = await _tools(broker)

    assert "set_voice" not in names
    assert "set_brain" in names


async def test_a_pinned_model_offers_neither_model_tool():
    broker = brain.build_broker(
        _settings(feature_model_control=False), knobs=Knobs(FakeSession())
    )
    names = await _tools(broker)

    assert set(names) == {"set_voice"}


# --- the property the whole design rests on ---------------------------------


async def test_a_change_lands_on_the_next_turn_not_this_one(monkeypatch):
    """`run_turn` reads `session.voice` into a local before the brain ever runs, so a
    tool that changes it mid-turn cannot switch voices between two sentences of the
    same reply. This asserts that directly, because it is the reason this design is
    safe at all rather than a happy accident."""
    import app.pipeline as pipeline

    session = FakeSession(VoiceSettings(speed=1.15))
    spoken_at: list[float] = []

    async def fake_synthesize(text, voice=None):
        spoken_at.append(voice.speed)
        return b"AUDIO"

    async def think_that_changes_the_voice(messages, system=None, **kwargs):
        # Exactly what the tool does, at exactly the moment it would happen.
        await Knobs(session).set_voice(speed=0.5)
        yield "First sentence. "
        yield "Second sentence."

    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    monkeypatch.setattr(pipeline, "think", think_that_changes_the_voice)
    monkeypatch.setattr(pipeline, "build_memory_view", lambda *a, **kw: _view())
    monkeypatch.setattr(pipeline, "remember", _nothing)

    async def send_json(payload):
        pass

    async def send_bytes(data):
        pass

    await pipeline.run_turn(
        None,
        send_json,
        send_bytes,
        text="hello",
        voice=session.voice,
        knobs=Knobs(session),
    )

    # Every sentence of this reply spoke at the old rate...
    assert spoken_at == [1.15, 1.15]
    # ...and the session carries the new one into the next turn.
    assert session.voice.speed == 0.5


async def _view():
    from app.memory import MemoryView

    return MemoryView()


async def _nothing(*a, **kw):
    return []


# --- the prompt has to name the current values ------------------------------


def test_describe_names_the_live_values(store):
    session = FakeSession(VoiceSettings(voice="nova", speed=1.15), keyword="strong")
    out = Knobs(session).describe()

    assert "nova" in out
    assert "1.15" in out
    assert "strong" in out


def test_the_settings_block_reaches_the_prompt():
    """Without the current speed in context, "slower" has nothing to be slower than."""
    from app.persona import compose_system_prompt

    prompt = compose_system_prompt(settings_description="speaking as nova at 1.15x")

    assert "Right now you're speaking as nova at 1.15x" in prompt


def test_the_settings_block_is_absent_when_there_is_nothing_to_say():
    """The standing guidance lives in CORE either way; only the *live values* are
    conditional, so an install with the tools off doesn't claim a speed it can't
    change."""
    from app.persona import compose_system_prompt

    assert "Right now you're" not in compose_system_prompt()


def test_these_tools_are_amber_s_own_not_signals():
    """They do real work, so `activity` should classify them ORIGIN_OWN. `expect_reply`
    is the only genuine no-op signal and the only member of that set."""
    from app.peers import classify_tool_name

    for name in ("set_voice", "set_brain", "remap_keyword"):
        assert classify_tool_name(name, ()) == protocol.ORIGIN_OWN


# --- the echo that keeps asking and clicking in step ------------------------


async def test_a_voice_change_echoes_the_ack_frame():
    """The mechanism the Settings page actually reads.

    A client's own `set_voice` is always answered with a `voice` frame, and the page
    renders that rather than its own request. A change Amber made herself has to answer
    the same way or the page sits showing a value that is no longer true.
    """
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    session = FakeSession(VoiceSettings(speed=1.15))
    await Knobs(session, send).set_voice(speed=0.9)

    frame = next(f for f in sent if f["type"] == protocol.VOICE)
    assert frame["settings"]["speed"] == 0.9
    # The catalogue too, so a picker built from the wire stays buildable.
    assert frame["options"]["voices"]


async def test_a_brain_change_echoes_the_model_frame(store):
    sent: list[dict] = []

    async def send(frame):
        sent.append(frame)

    session = FakeSession()
    await Knobs(session, send).set_brain("strong")

    frame = next(f for f in sent if f["type"] == protocol.MODEL)
    assert frame["settings"]["keyword"] == "strong"


async def test_a_dead_socket_costs_the_echo_and_not_the_change():
    async def send(frame):
        raise RuntimeError("socket is gone")

    session = FakeSession()
    out = await Knobs(session, send).set_voice(speed=0.8)

    assert session.voice.speed == 0.8
    assert "0.8" in out


async def test_knobs_work_with_no_socket_at_all():
    """A turn driven from a test or a script has no sender; the tools still work."""
    session = FakeSession()
    await Knobs(session).set_voice(speed=0.8)
    assert session.voice.speed == 0.8


def test_the_guidance_is_absent_when_the_tools_are():
    """Telling the model to call `set_voice` on an install that never offers it is a
    prompt describing a capability that does not exist — the exact rot the composed
    persona was built to end."""
    from app.persona import compose_system_prompt

    assert "set_voice" not in compose_system_prompt()
    assert "set_voice" in compose_system_prompt(settings_description="speaking as nova")
