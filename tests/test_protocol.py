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


# --- push / confirm_request -------------------------------------------------
#
# The shape discipline every factory in this module keeps: an optional key is attached
# only when it applies, never as a null placeholder, so a client can read "absent" as
# "not known" rather than having to tell `None` from missing.


def test_push_bare_shape():
    assert protocol.push("p_1", protocol.PUSH_NOTICE, "hello") == {
        "type": protocol.PUSH,
        "id": "p_1",
        "kind": protocol.PUSH_NOTICE,
        "text": "hello",
    }


def test_push_omits_optional_keys_rather_than_nulling_them():
    frame = protocol.push("p_1", protocol.PUSH_NOTICE, "hello", ref=None, title=None)
    assert "ref" not in frame
    assert "title" not in frame
    assert "created_at" not in frame


def test_push_carries_what_it_was_given():
    frame = protocol.push(
        "p_1",
        protocol.PUSH_REMINDER,
        "call the dentist",
        created_at="2026-08-17T23:30:00+00:00",
        ref={"reminder_id": 12},
        title="Reminder",
    )
    assert frame["ref"] == {"reminder_id": 12}
    assert frame["title"] == "Reminder"
    assert frame["created_at"] == "2026-08-17T23:30:00+00:00"


def test_push_copies_its_ref():
    """Defensive copies on the way out, like every other factory here."""
    ref = {"reminder_id": 12}
    frame = protocol.push("p_1", protocol.PUSH_REMINDER, "x", ref=ref)
    ref["reminder_id"] = 99
    assert frame["ref"] == {"reminder_id": 12}


def test_confirm_request_shape():
    frame = protocol.confirm_request(
        "k1", "update_server", origin=protocol.ORIGIN_OWN, tool_input={}
    )
    assert frame == {
        "type": protocol.CONFIRM_REQUEST,
        "id": "k1",
        "name": "update_server",
        "origin": protocol.ORIGIN_OWN,
        "input": {},
    }


def test_confirm_request_omits_input_when_not_given():
    frame = protocol.confirm_request("k1", "update_server", origin=protocol.ORIGIN_OWN)
    assert "input" not in frame
