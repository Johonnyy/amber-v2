"""Tests for the WS protocol frame factories (turn-based additive field)."""

from app import protocol


def test_turn_complete_bare_shape_unchanged():
    # The common case must stay the exact {"type", "sentences"} shape old clients expect.
    assert protocol.turn_complete(2) == {"type": protocol.TURN_COMPLETE, "sentences": 2}


def test_turn_complete_omits_awaiting_when_false():
    assert "awaiting_response" not in protocol.turn_complete(1, awaiting_response=False)


def test_turn_complete_carries_awaiting_when_true():
    frame = protocol.turn_complete(2, awaiting_response=True)
    assert frame["sentences"] == 2
    assert frame["awaiting_response"] is True
