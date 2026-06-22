"""A small sliding-window rate limiter (Phase 5).

One instance guards one thing — here, the utterances on a single session. It keeps
the timestamps of recent events in a window and refuses a new one once the window
is full. The cost of an LLM voice turn (STT + LLM + TTS) is real money, so this is
a cost guardrail as much as an abuse guard: a stuck client that floods audio is
capped instead of running up a bill.

The limiter is event-loop-confined (one session, touched only from its WS handler),
so it needs no locking. ``now`` is injectable for deterministic tests; in
production it defaults to a monotonic clock, which is immune to wall-clock jumps.
"""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    """Allow at most ``max_events`` events per ``window_s`` seconds.

    ``max_events <= 0`` disables the limit (every event is allowed and nothing is
    tracked), so a single flag can turn guarding off.
    """

    def __init__(self, max_events: int, window_s: float) -> None:
        self.max_events = max_events
        self.window_s = window_s
        self._events: deque[float] = deque()

    def _purge(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def allow(self, now: float | None = None) -> bool:
        """Record and allow an event, or refuse it if the window is full."""
        if self.max_events <= 0:
            return True
        now = time.monotonic() if now is None else now
        self._purge(now)
        if len(self._events) >= self.max_events:
            return False
        self._events.append(now)
        return True

    def retry_after(self, now: float | None = None) -> float:
        """Seconds until the oldest event leaves the window (0 if room now)."""
        if self.max_events <= 0 or not self._events:
            return 0.0
        now = time.monotonic() if now is None else now
        return max(0.0, self._events[0] + self.window_s - now)
