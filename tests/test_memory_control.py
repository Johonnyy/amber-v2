"""Curating memory from a client, not just from the model.

The point of these is that the UI path and the tool path are the *same* path. A fact
Amber forgets because she was asked out loud and one forgotten with a button have to
end up as the same row in the same state, or the two halves of memory drift and the
panel starts lying.
"""

import pytest

from app import memory_control, protocol
from app.config import Settings
from app.memory.store import STATUS_ACTIVE, MemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = MemoryStore(str(tmp_path / "amber.db"))
    monkeypatch.setattr("app.memory_control.get_store", lambda: db)
    return db


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


async def test_forget_soft_deletes_and_answers_with_what_is_left(store):
    keep = store.add_fact("Lives in Denver")
    drop = store.add_fact("Lives in Boston")

    frame = await memory_control.handle_action(
        {"action": "forget", "id": drop}, _settings()
    )

    assert frame["type"] == protocol.MEMORY
    assert frame["ack"] == {
        "action": "forget",
        "id": drop,
        "ok": True,
        "content": "Lives in Boston",
    }
    assert [f["id"] for f in frame["facts"]] == [keep]
    # Soft: the row is still there, which is what makes the undo below honest
    # rather than a promise about something already gone.
    assert store.get_fact(drop) is not None


async def test_forget_can_be_undone(store):
    fact_id = store.add_fact("Lives in Boston")
    await memory_control.handle_action({"action": "forget", "id": fact_id}, _settings())

    frame = await memory_control.handle_action(
        {"action": "restore", "id": fact_id}, _settings()
    )

    assert frame["ack"]["ok"] is True
    assert store.get_fact(fact_id)["status"] == STATUS_ACTIVE
    assert [f["id"] for f in frame["facts"]] == [fact_id]


async def test_correct_supersedes_rather_than_overwrites(store):
    old = store.add_fact("Lives in Boston")

    frame = await memory_control.handle_action(
        {"action": "correct", "id": old, "content": "Lives in Denver"}, _settings()
    )

    assert frame["ack"]["ok"] is True
    # The old row survives, marked and pointing at its replacement. Overwriting
    # would throw away the one thing that makes a correction auditable.
    previous = store.get_fact(old)
    assert previous["status"] == "superseded"
    assert previous["superseded_by"] is not None
    assert [f["content"] for f in frame["facts"]] == ["Lives in Denver"]


async def test_a_refused_action_still_answers(store):
    """Silence is indistinguishable from a dropped socket, so never be silent."""
    frame = await memory_control.handle_action(
        {"action": "forget", "id": 9999}, _settings()
    )
    assert frame["ack"]["ok"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "explode", "id": 1},
        {"action": "forget"},
        {"action": "forget", "id": "1"},
        {},
    ],
)
async def test_malformed_actions_are_refused_not_obeyed(store, payload):
    store.add_fact("Lives in Boston")
    frame = await memory_control.handle_action(payload, _settings())
    assert frame["ack"]["ok"] is False


async def test_correct_without_content_changes_nothing(store):
    fact_id = store.add_fact("Lives in Boston")
    frame = await memory_control.handle_action(
        {"action": "correct", "id": fact_id, "content": "   "}, _settings()
    )
    assert frame["ack"]["ok"] is False
    assert store.get_fact(fact_id)["status"] == STATUS_ACTIVE


async def test_query_browses_everything_not_just_this_turn(store):
    for i in range(5):
        store.add_fact(f"Fact number {i}")

    frame = await memory_control.handle_query({}, _settings())

    assert frame["scope"] == "browse"
    assert frame["total"] == 5
    assert len(frame["facts"]) == 5


async def test_query_searches_when_given_words(store):
    store.add_fact("Lives in Denver")
    store.add_fact("Drives a blue van")

    frame = await memory_control.handle_query({"q": "Denver"}, _settings())

    assert [f["content"] for f in frame["facts"]] == ["Lives in Denver"]
    # `total` is the whole store, not the result count — it is what a panel says it
    # is showing a slice *of*.
    assert frame["total"] == 2


async def test_a_browse_is_capped(store):
    for i in range(5):
        store.add_fact(f"Fact number {i}")

    frame = await memory_control.handle_query({"limit": 10_000}, _settings())

    assert len(frame["facts"]) <= memory_control.MAX_BROWSE_LIMIT


async def test_the_turn_frame_shape_is_untouched(store):
    """`scope` and friends ride only on the frames that need them.

    A client written against the Phase-3 `memory` frame must see exactly what it saw
    before, or "additive" is not what happened.
    """
    plain = protocol.memory(["a fact"])
    assert plain == {"type": "memory", "items": ["a fact"]}


def test_the_flag_gates_it(store):
    assert memory_control.enabled(_settings()) is True
    assert memory_control.enabled(_settings(feature_memory_control=False)) is False
    # Memory off takes the control surface with it — there is nothing to curate.
    assert memory_control.enabled(_settings(feature_memory=False)) is False
