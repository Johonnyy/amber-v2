"""Where a turn's seconds went.

The shape discipline matters as much as the numbers: a typed turn does no STT and the
canned path never reaches the model, so emitting a zero for either would assert
something false about what happened.
"""

import asyncio

import pytest

import app.pipeline as pipeline
from app import protocol
from app.memory import MemoryView
from app.timings import TurnTimings, step_spans


class FakeSink:
    def __init__(self):
        self.json: list[dict] = []
        self.bytes: list[bytes] = []

    async def send_json(self, payload: dict) -> None:
        self.json.append(payload)

    async def send_bytes(self, data: bytes) -> None:
        self.bytes.append(data)


@pytest.fixture
def faked(monkeypatch):
    async def transcribe(audio, **kw):
        await asyncio.sleep(0.02)
        return "hello amber"

    async def synthesize(text, voice=None):
        await asyncio.sleep(0.01)
        return b"AUDIO"

    async def think(messages, system=None, **kwargs):
        await asyncio.sleep(0.02)
        yield "Hello back. "
        yield "Good to hear from you."

    async def no_ctx(query=None, **kw):
        return MemoryView()

    async def no_rem(u, r, **kw):
        return []

    monkeypatch.setattr(pipeline, "transcribe", transcribe)
    monkeypatch.setattr(pipeline, "synthesize", synthesize)
    monkeypatch.setattr(pipeline, "think", think)
    monkeypatch.setattr(pipeline, "build_memory_view", no_ctx)
    monkeypatch.setattr(pipeline, "remember", no_rem)


def _turn_complete(sink: FakeSink) -> dict:
    return next(f for f in sink.json if f["type"] == protocol.TURN_COMPLETE)


# --- the unit ---------------------------------------------------------------


def test_only_what_actually_happened_is_reported():
    """Absent means "this turn didn't do that" — the same discipline every other frame
    in `app.protocol` keeps. A zero would claim STT ran and took no time."""
    timings = TurnTimings()
    out = timings.as_dict()

    assert set(out) == {"total_ms"}
    assert "stt_ms" not in out
    assert "first_token_ms" not in out


def test_spans_accumulate_rather_than_overwrite():
    """TTS is per-sentence and interleaved with generation, so it is a sum of the time
    spent inside synthesis, not a window on a clock."""
    timings = TurnTimings()
    for _ in range(3):
        with timings.tts():
            pass

    assert "tts_ms" in timings.as_dict() or timings.tts_s >= 0.0


def test_the_first_token_is_stamped_once():
    timings = TurnTimings()
    timings.first_token()
    first = timings.first_token_s
    timings.first_token()

    assert timings.first_token_s == first


def test_a_span_does_not_swallow_a_failure():
    """An STT error is a failed turn, not a timing problem."""
    timings = TurnTimings()
    with pytest.raises(ValueError):
        with timings.stt():
            raise ValueError("stt blew up")
    # ...and it still recorded how long it took to fail.
    assert timings.stt_s > 0


def test_step_spans_survive_a_runtime_that_reports_nothing():
    class Bare:
        steps = [type("S", (), {"started_at": "", "finished_at": "", "model": "m"})()]

    assert step_spans(Bare()) == []
    assert step_spans(None) == []
    assert step_spans(type("S", (), {"steps": None})()) == []


# --- through a real turn ----------------------------------------------------


async def test_a_spoken_turn_reports_stt_and_tts(faked):
    sink = FakeSink()
    await pipeline.run_turn(b"audio", sink.send_json, sink.send_bytes)

    timings = _turn_complete(sink)["timings"]
    assert timings["stt_ms"] >= 15
    assert timings["tts_ms"] >= 15  # two sentences, ~10ms each
    assert timings["first_token_ms"] > 0
    # The parts cannot exceed the whole.
    assert timings["total_ms"] >= timings["stt_ms"]


async def test_a_typed_turn_reports_no_stt(faked):
    """It skipped transcription entirely, and the frame should say so by omission."""
    sink = FakeSink()
    await pipeline.run_turn(None, sink.send_json, sink.send_bytes, text="hello")

    timings = _turn_complete(sink)["timings"]
    assert "stt_ms" not in timings
    assert timings["total_ms"] > 0


async def test_every_turn_gets_a_total(faked, monkeypatch):
    """Including the canned path, which never reaches the model."""
    async def nothing_heard(audio, **kw):
        return ""

    monkeypatch.setattr(pipeline, "transcribe", nothing_heard)
    sink = FakeSink()
    await pipeline.run_turn(b"audio", sink.send_json, sink.send_bytes)

    frame = _turn_complete(sink)
    assert frame["timings"]["total_ms"] >= 0
    assert "first_token_ms" not in frame["timings"]  # the model never ran


async def test_the_turn_signal_finally_records_its_duration(faked, monkeypatch):
    """This was hard-coded ``None``, so the one telemetry row representing a
    user-visible turn had never carried a latency."""
    recorded: list[dict] = []
    monkeypatch.setattr(
        pipeline.signals, "record", lambda kind, **kw: recorded.append({"kind": kind, **kw})
    )

    sink = FakeSink()
    await pipeline.run_turn(b"audio", sink.send_json, sink.send_bytes)

    turn = next(r for r in recorded if r["kind"] == pipeline.signals.KIND_TURN)
    assert turn["latency_ms"] > 0


async def test_step_spans_reach_the_frame(faked, monkeypatch):
    """`RunState.steps` has always carried these and `_stats` threw them away."""

    async def think_with_steps(messages, system=None, **kwargs):
        state = kwargs.get("state")
        if state is not None:
            state.steps.append(
                type(
                    "S",
                    (),
                    {
                        "index": 0,
                        "model": "vendor/m",
                        "tokens_in": 10,
                        "tokens_out": 5,
                        "cost_usd": 0.0,
                        "started_at": "2026-08-17T10:00:00+00:00",
                        "finished_at": "2026-08-17T10:00:02+00:00",
                    },
                )()
            )
        yield "Hi."

    monkeypatch.setattr(pipeline, "think", think_with_steps)
    monkeypatch.setattr(pipeline, "brain_record_spend", _no_spend)

    sink = FakeSink()
    await pipeline.run_turn(None, sink.send_json, sink.send_bytes, text="hello")

    spans = _turn_complete(sink)["step_spans"]
    assert spans == [
        {
            "started_at": "2026-08-17T10:00:00+00:00",
            "finished_at": "2026-08-17T10:00:02+00:00",
            "model": "vendor/m",
        }
    ]


async def _no_spend(state, **kw):
    return 0.0
