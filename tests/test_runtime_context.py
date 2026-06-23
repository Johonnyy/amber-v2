"""Tests for the ambient runtime-context block (date/time on every prompt)."""

from datetime import datetime, timezone

from app.config import Settings
from app.runtime_context import _resolve_tz, build_runtime_context


def _settings(**over):
    return Settings(_env_file=None, **over)


def test_formats_date_and_time_for_the_ear():
    # A fixed instant so the assertion is stable: Tue 23 Jun 2026, 15:45 UTC.
    now = datetime(2026, 6, 23, 15, 45, tzinfo=timezone.utc)
    block = build_runtime_context(settings=_settings(), now=now)
    assert "Tuesday, June 23, 2026" in block
    assert "3:45 PM" in block  # 12-hour, with a leading-zero-free hour
    assert "UTC" in block


def test_midnight_and_noon_use_12_hour_clock():
    midnight = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert "12:05 AM" in build_runtime_context(settings=_settings(), now=midnight)
    assert "12:00 PM" in build_runtime_context(settings=_settings(), now=noon)


def test_always_returns_a_non_empty_block():
    # No injected clock: still produces a stamp (read from the wall clock in UTC).
    block = build_runtime_context(settings=_settings(timezone="UTC"))
    assert block and "Right now it's" in block


def test_unknown_timezone_falls_back_to_utc():
    assert _resolve_tz("Not/AZone") is timezone.utc
    assert _resolve_tz("") is timezone.utc
    assert _resolve_tz("UTC") is timezone.utc
