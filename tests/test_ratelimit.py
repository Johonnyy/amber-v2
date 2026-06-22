"""Tests for the sliding-window rate limiter (clock injected for determinism)."""

from app.ratelimit import RateLimiter


def test_allows_up_to_limit_then_refuses():
    rl = RateLimiter(max_events=3, window_s=60)
    assert rl.allow(now=0) is True
    assert rl.allow(now=1) is True
    assert rl.allow(now=2) is True
    assert rl.allow(now=3) is False  # 4th within the window


def test_window_slides_and_frees_capacity():
    rl = RateLimiter(max_events=2, window_s=10)
    assert rl.allow(now=0) is True
    assert rl.allow(now=1) is True
    assert rl.allow(now=5) is False  # full
    # The first event (t=0) leaves the 10s window at t>10.
    assert rl.allow(now=11) is True


def test_retry_after_reports_wait():
    rl = RateLimiter(max_events=1, window_s=10)
    assert rl.allow(now=0) is True
    assert rl.allow(now=3) is False
    # Oldest event at t=0 frees at t=10, so 7s left from t=3.
    assert rl.retry_after(now=3) == 7


def test_zero_limit_disables_guard():
    rl = RateLimiter(max_events=0, window_s=10)
    for t in range(100):
        assert rl.allow(now=t) is True
    assert rl.retry_after(now=0) == 0.0
