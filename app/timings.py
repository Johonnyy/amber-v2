"""Where a turn's seconds actually went.

Amber could always tell you a turn took eight seconds and never where they went. The
`activity` frame reports each tool call's duration, so the tool portion has been visible
since it landed — but STT, the wait for the first token, and synthesis were never
measured anywhere, and those are the three that decide whether she *feels* slow. A turn
that spent six seconds in a peer call and one in TTS is a completely different problem
from one that spent six in TTS, and until now both looked identical from outside.

Two `perf_counter` pairs and a first-token stamp are the whole of it. Per-model-step
times need no new instrumentation at all: `agent_runtime`'s `Step` has carried
``started_at``/``finished_at`` from the beginning and `_stats` simply threw them away.

**Deliberately incomplete, and the client finishes it.** There is no `tools_ms` here.
The pipeline cannot see tool calls — they happen inside the broker, several layers down —
and threading a timer through `build_broker` to collect what the `activity` frames
already carry would be measuring the same thing twice, in two places, with two chances to
disagree. The server reports what only the server knows; the client already has the rest.

Nothing here is on the latency path in any meaningful sense: it is subtraction, and it is
wrapped where it could plausibly fail, because a turn must never break over a number that
only draws a chart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def _ms(seconds: float) -> int:
    return max(0, int(seconds * 1000))


@dataclass
class TurnTimings:
    """Stopwatches for one turn, filled in as it runs.

    Mutable and passed by reference, like `TurnSignals` — the pieces that know each
    duration are scattered across the pipeline, and handing each one a return value to
    thread back would be a lot of plumbing for a chart.
    """

    started: float = field(default_factory=time.perf_counter)
    stt_s: float = 0.0
    #: From the start of the turn to the first text out of the model. The number that
    #: most closely matches "how long before she started talking".
    first_token_s: float | None = None
    #: Summed across every sentence, since synthesis is per-sentence and interleaved
    #: with generation — this is total time *in* TTS, not a span on a clock.
    tts_s: float = 0.0

    def stt(self) -> "_Span":
        return _Span(self, "stt_s")

    def tts(self) -> "_Span":
        return _Span(self, "tts_s")

    def first_token(self) -> None:
        """Stamp the first token, if it hasn't been stamped already."""
        if self.first_token_s is None:
            self.first_token_s = time.perf_counter() - self.started

    @property
    def total_s(self) -> float:
        return time.perf_counter() - self.started

    def as_dict(self) -> dict[str, Any]:
        """The wire shape, in milliseconds. Only what was actually measured.

        A typed turn skips STT entirely and the canned path never reaches the model, so
        emitting a zero for either would assert something false — absent means "this
        turn didn't do that", which is the same shape discipline every other frame in
        `app.protocol` keeps.
        """
        out: dict[str, Any] = {"total_ms": _ms(self.total_s)}
        if self.stt_s:
            out["stt_ms"] = _ms(self.stt_s)
        if self.first_token_s is not None:
            out["first_token_ms"] = _ms(self.first_token_s)
        if self.tts_s:
            out["tts_ms"] = _ms(self.tts_s)
        return out


class _Span:
    """Context manager adding its elapsed time to one field of a `TurnTimings`."""

    __slots__ = ("_timings", "_field", "_start")

    def __init__(self, timings: TurnTimings, field_name: str) -> None:
        self._timings = timings
        self._field = field_name
        self._start = 0.0

    def __enter__(self) -> "_Span":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        elapsed = time.perf_counter() - self._start
        setattr(self._timings, self._field, getattr(self._timings, self._field) + elapsed)
        # Never swallow: an STT failure is a failed turn, not a timing problem.
        return None


def step_spans(state: Any) -> list[dict[str, Any]]:
    """Each model call's start/end, for the waterfall's model bars.

    Read off `RunState.steps`, which has always carried them. Defensive because this is
    a chart: a runtime that stopped populating the fields, or a fake in a test, must cost
    the bars and not the turn.
    """
    spans: list[dict[str, Any]] = []
    for step in getattr(state, "steps", None) or []:
        try:
            started, finished = step.started_at, step.finished_at
            if not started or not finished:
                continue
            spans.append(
                {"started_at": started, "finished_at": finished, "model": step.model}
            )
        except Exception:  # noqa: BLE001 — a missing field is not a failed turn
            continue
    return spans
