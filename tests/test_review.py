"""Putting a human back in the loop — tool reliability, reflections, eval cases.

Everything under test here reads data Amber has always recorded and never shown anyone,
so most of these tests are less about new behaviour than about proving the data was
really there all along and now comes out in a usable shape.
"""

import pytest

import app.memory_control as memory_control
import app.review as review
from app import protocol
from app.config import Settings
from app.memory.store import MemoryStore


def _settings(**over):
    base = {"_env_file": None, "feature_review": True, "feature_evals": True}
    return Settings(**{**base, **over})


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore(":memory:")
    monkeypatch.setattr(review, "get_store", lambda: s)
    monkeypatch.setattr(memory_control, "get_store", lambda: s)
    yield s
    s.close()


def _tool_call(store, name, ok, ms):
    store.add_turn_events(
        [
            {
                "kind": "tool_call",
                "name": name,
                "ok": 1 if ok else 0,
                "latency_ms": ms,
                "detail": None,
                "session_id": None,
            }
        ]
    )


# --- tools ------------------------------------------------------------------


def test_the_kind_constant_agrees_with_signals():
    """`store` spells the constant out rather than importing it, because `app.signals`
    imports the store and reaching back would cycle. This is the link between them."""
    from app.memory.store import _KIND_TOOL_CALL
    from app.signals import KIND_TOOL_CALL

    assert _KIND_TOOL_CALL == KIND_TOOL_CALL


def test_reliability_reports_a_rate_and_percentiles(store):
    for _ in range(3):
        _tool_call(store, "web_search", True, 100)
    _tool_call(store, "web_search", False, 9000)

    rows = store.tool_reliability("2000-01-01T00:00:00+00:00")
    search = next(r for r in rows if r["name"] == "web_search")

    assert search["calls"] == 4
    assert search["errors"] == 1
    assert search["ok_rate"] == 0.75
    # An average would hide the 9s outlier behind three fast calls, which is exactly
    # the tail a degraded tool feels like.
    assert search["p95_ms"] == 9000
    assert search["p50_ms"] < search["p95_ms"]


def test_percentiles_match_the_mcp_layer(store):
    """Both tables are read side by side; disagreeing about p95 would be worse than
    not reporting it."""
    from agent_mcp.usage_log import _percentile as theirs

    from app.memory.store import _percentile as ours

    values = [5, 10, 20, 40, 80, 160]
    for pct in (50, 90, 95, 99):
        assert ours(values, pct) == theirs(values, pct)
    assert ours([], 95) == theirs([], 95)


def test_an_unknown_outcome_is_not_a_failure(store):
    """``ok`` is nullable, and NULL means "not recorded" rather than "it broke"."""
    store.add_turn_events(
        [{"kind": "tool_call", "name": "x", "ok": None, "latency_ms": 5,
          "detail": None, "session_id": None}]
    )
    row = store.tool_reliability("2000-01-01T00:00:00+00:00")[0]

    assert row["errors"] == 0
    assert row["ok_rate"] == 1.0


def test_the_worst_tool_sorts_first(store):
    _tool_call(store, "good", True, 10)
    _tool_call(store, "good", True, 10)
    _tool_call(store, "broken", False, 10)

    rows = store.tool_reliability("2000-01-01T00:00:00+00:00")
    assert rows[0]["name"] == "broken"


async def test_the_tools_topic_answers_with_a_window(store):
    _tool_call(store, "web_search", True, 100)
    frame = await review.handle_query({"topic": "tools"}, _settings())

    assert frame["type"] == protocol.REVIEW
    assert frame["topic"] == protocol.REVIEW_TOOLS
    assert frame["items"][0]["name"] == "web_search"
    assert frame["since"]  # says what it is a slice of


# --- reflections ------------------------------------------------------------


async def test_reflections_can_be_promoted_into_a_durable_fact(store):
    """The verb that makes AMBER_FEATURE_SELF_NOTES safe to leave off: the value of
    self-observation, without the model editing its own instructions."""
    rid = store.add_reflection("Replies run long in the evening")

    frame = await review.handle_action(
        {"topic": "reflections", "action": "promote", "id": rid}, _settings()
    )

    assert frame["ack"]["ok"] is True
    facts = [f["content"] for f in store.active_facts()]
    assert "Replies run long in the evening" in facts
    # Promoted by a *person*, so it carries the same provenance a "remember this" does.
    row = next(f for f in store.active_facts() if f["content"].startswith("Replies"))
    assert row["source"] == "explicit"
    assert row["tier"] == "durable"
    # And it leaves the list, so the same observation can't be promoted twice.
    assert store.recent_reflections() == []


async def test_reflections_can_be_dismissed(store):
    """`dismissed` has existed since the table was created and had no writer at all."""
    rid = store.add_reflection("Something not worth keeping")

    frame = await review.handle_action(
        {"topic": "reflections", "action": "dismiss", "id": rid}, _settings()
    )

    assert frame["ack"]["ok"] is True
    assert store.recent_reflections() == []
    assert store.active_facts() == []  # dismissing is not promoting


def test_recent_reflections_returns_the_dismissed_column(store):
    """It was filtered on and not selected, so a dismissal was invisible even once
    written."""
    store.add_reflection("A note")
    assert "dismissed" in store.recent_reflections()[0]


async def test_a_refused_action_still_answers(store):
    """A panel that clicked something needs to know it did not take; silence is
    indistinguishable from a dropped socket."""
    frame = await review.handle_action(
        {"topic": "reflections", "action": "dismiss", "id": 9999}, _settings()
    )
    assert frame["ack"]["ok"] is False


async def test_a_malformed_action_is_answered_not_crashed(store):
    frame = await review.handle_action({"topic": "nonsense"}, _settings())
    assert frame["type"] == protocol.REVIEW
    assert frame["ack"]["ok"] is False


# --- evals ------------------------------------------------------------------


async def test_a_turn_can_be_saved_as_a_case(store):
    case_id = await review.capture_eval(
        {
            "query": "remind me to call the dentist",
            "expect_tool": "set_reminder",
            "got_tool": "web_search",
            "note": "searched the web instead",
            "reply": "Here's what I found...",
        }
    )

    assert case_id is not None
    case = store.eval_cases()[0]
    assert case["query"] == "remind me to call the dentist"
    assert case["expect_tool"] == "set_reminder"
    assert case["got_tool"] == "web_search"


async def test_a_case_needs_something_to_replay(store):
    assert await review.capture_eval({"query": "   "}) is None
    assert await review.capture_eval({}) is None
    assert store.eval_cases() == []


async def test_a_case_can_be_archived_without_being_lost(store):
    case_id = await review.capture_eval({"query": "hello"})
    frame = await review.handle_action(
        {"topic": "evals", "action": "archive", "id": case_id}, _settings()
    )

    assert frame["ack"]["ok"] is True
    assert store.eval_cases() == []
    assert store.eval_cases(status="archived")[0]["query"] == "hello"


def test_a_case_result_passes_only_when_the_expected_tool_was_called():
    from app.evals import CaseResult

    assert CaseResult(1, "q", "set_reminder", called=["set_reminder"]).passed
    assert not CaseResult(1, "q", "set_reminder", called=["web_search"]).passed
    # A case with no expectation still asserts that the turn didn't blow up — plenty
    # of bad turns are bad without a right answer anyone can name.
    assert CaseResult(1, "q", None, called=[]).passed
    assert not CaseResult(1, "q", None, error="boom").passed


def test_export_is_valid_jsonl(store, capsys):
    import json

    from app.evals import _export

    store.add_eval_case("remind me", expect_tool="set_reminder")
    _export(store)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert json.loads(lines[0])["query"] == "remind me"


# --- fact lineage and the archive -------------------------------------------


async def test_lineage_walks_the_whole_chain_from_any_link(store):
    """`superseded_by` has been written on every correction and read by nothing."""
    a = store.add_fact("Lives in Boston")
    b = store.supersede_fact(a, "Lives in Denver")
    c = store.supersede_fact(b, "Lives in Portland")

    for asked_about in (a, b, c):
        frame = await memory_control.handle_query(
            {"scope": "lineage", "id": asked_about}, _settings()
        )
        assert frame["scope"] == "lineage"
        assert [f["content"] for f in frame["facts"]] == [
            "Lives in Boston",
            "Lives in Denver",
            "Lives in Portland",
        ]


async def test_lineage_of_a_fact_with_no_history_is_just_the_fact(store):
    fact_id = store.add_fact("Prefers tea")
    frame = await memory_control.handle_query(
        {"scope": "lineage", "id": fact_id}, _settings()
    )
    assert [f["content"] for f in frame["facts"]] == ["Prefers tea"]


async def test_lineage_survives_a_restored_fact_pointing_at_its_replacement(store):
    """`update_fact(status='active')` restores a superseded row without clearing its
    forward pointer, so a chain is not acyclic just because well-behaved code wrote
    it. Unbounded, this walk would hang."""
    a = store.add_fact("First")
    b = store.supersede_fact(a, "Second")
    store.update_fact(a, status="active")

    frame = await memory_control.handle_query({"scope": "lineage", "id": b}, _settings())
    assert len(frame["facts"]) <= 12


async def test_the_archive_shows_what_she_no_longer_believes(store):
    kept = store.add_fact("Still true")
    gone = store.add_fact("Not any more")
    store.forget_fact(gone)
    superseded = store.add_fact("Old address")
    store.supersede_fact(superseded, "New address")

    frame = await memory_control.handle_query({"scope": "archive"}, _settings())

    contents = [f["content"] for f in frame["facts"]]
    assert "Not any more" in contents
    assert "Old address" in contents
    assert "Still true" not in contents
    assert store.get_fact(kept)["status"] == "active"


def test_the_policy_a_countdown_needs_is_on_the_status_frame():
    """A client computing "forgotten in 4 days" from hardcoded thresholds would be
    quietly wrong on any install that tuned them."""
    import asyncio

    from app import status as status_report
    from app.session import get_session_manager

    session, _ = get_session_manager().resume_or_create(None)
    sections = asyncio.run(status_report.build(session, _settings()))
    policy = sections["memory"]["policy"]

    assert policy["short_ttl_days"] == 30.0
    assert policy["session_ttl_hours"] == 12.0
    assert policy["promote_uses"] == 3
    # A short fact used twice is immune to decay but still not durable — a UI showing
    # only the promotion threshold would report a countdown for a fact in no danger.
    assert policy["decay_immune_uses"] == 2
    # And removal happens on the next maintenance pass, not at the instant a deadline
    # passes, so a countdown implying otherwise would be wrong by up to one interval.
    assert policy["pass_interval_s"] > 0


# --- what the review pass caught --------------------------------------------


def test_the_status_frame_really_reports_the_archive_counts(store, monkeypatch):
    """`fact_count` is keyword-only, and `status` gathers each section inside a
    swallow-everything guard — so passing it positionally lost the whole memory
    section silently, counts and policy together."""
    import asyncio

    from app import status as status_report
    from app.session import get_session_manager

    monkeypatch.setattr("app.memory.store.get_store", lambda: store)
    gone = store.add_fact("Not any more")
    store.forget_fact(gone)
    store.add_fact("Still true")

    session, _ = get_session_manager().resume_or_create(None)
    memory = asyncio.run(status_report.build(session, _settings()))["memory"]

    assert memory["forgotten"] == 1
    assert memory["facts"] == 1
    assert "policy" in memory


async def test_the_archive_reports_the_real_total_not_the_page_size(store):
    for i in range(5):
        fact_id = store.add_fact(f"gone {i}")
        store.forget_fact(fact_id)

    frame = await memory_control.handle_query(
        {"scope": "archive", "limit": 2}, _settings()
    )

    assert len(frame["facts"]) == 2
    # "2 of 2" would tell a truncated list it was whole.
    assert frame["total"] == 5


async def test_the_evals_topic_is_silent_when_the_feature_is_off(store):
    await review.capture_eval({"query": "hello"})
    frame = await review.handle_query(
        {"topic": "evals"}, _settings(feature_evals=False)
    )

    # Still answers the topic that was asked, so a client switching tabs gets an empty
    # panel rather than someone else's data under the wrong heading.
    assert frame["topic"] == protocol.REVIEW_EVALS
    assert frame["items"] == []


async def test_an_eval_case_cannot_be_archived_when_the_feature_is_off(store):
    case_id = await review.capture_eval({"query": "hello"})
    frame = await review.handle_action(
        {"topic": "evals", "action": "archive", "id": case_id},
        _settings(feature_evals=False),
    )
    assert frame["ack"]["ok"] is False
