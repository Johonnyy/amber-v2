"""Tests for Conversation trimming and the SessionManager (reconnect/TTL/caps)."""

from app.session import Conversation, SessionManager


# --- Conversation history cap ---

def test_conversation_unbounded_by_default():
    c = Conversation()
    for i in range(10):
        c.add_user(f"u{i}")
        c.add_assistant(f"a{i}")
    assert len(c.messages) == 20


def test_conversation_trims_to_cap_and_starts_on_user():
    c = Conversation(max_messages=4)
    c.add_user("u1")
    c.add_assistant("a1")
    c.add_user("u2")
    c.add_assistant("a2")
    c.add_user("u3")  # pushes over the cap of 4
    c.add_assistant("a3")
    assert len(c.messages) <= 4
    assert c.messages[0]["role"] == "user"  # never leads with an assistant turn
    assert c.messages[-1] == {"role": "assistant", "content": "a3"}


# --- SessionManager ---

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _manager(clock, **over):
    base = dict(
        ttl_s=100.0,
        max_sessions=10,
        max_messages=None,
        rate_limit_turns=30,
        rate_limit_window_s=60.0,
        clock=clock,
    )
    base.update(over)
    return SessionManager(**base)


def test_create_mints_new_id_and_not_resumed():
    mgr = _manager(FakeClock())
    session, resumed = mgr.resume_or_create(None)
    assert resumed is False
    assert session.id
    assert mgr.count() == 1


def test_resume_returns_same_session():
    clock = FakeClock()
    mgr = _manager(clock)
    first, _ = mgr.resume_or_create(None)
    first.conversation.add_user("hello")

    clock.t = 10.0
    again, resumed = mgr.resume_or_create(first.id)
    assert resumed is True
    assert again is first
    assert again.conversation.messages[0]["content"] == "hello"
    assert mgr.count() == 1  # not a new session


def test_unknown_id_creates_fresh_session():
    mgr = _manager(FakeClock())
    session, resumed = mgr.resume_or_create("bogus-id-not-issued")
    assert resumed is False
    assert session.id != "bogus-id-not-issued"  # server-minted, not client-chosen


def test_expired_session_starts_fresh():
    clock = FakeClock()
    mgr = _manager(clock, ttl_s=100.0)
    first, _ = mgr.resume_or_create(None)
    first.conversation.add_user("old context")

    clock.t = 101.0  # just past the TTL
    revived, resumed = mgr.resume_or_create(first.id)
    assert resumed is False  # expired -> brand new
    assert revived.id != first.id
    assert revived.conversation.messages == []


def test_capacity_evicts_least_recently_active():
    clock = FakeClock()
    mgr = _manager(clock, max_sessions=2)
    a, _ = mgr.resume_or_create(None)
    clock.t = 1.0
    b, _ = mgr.resume_or_create(None)
    clock.t = 2.0
    c, _ = mgr.resume_or_create(None)  # over cap -> evict the oldest (a)

    assert mgr.count() == 2
    assert mgr.get(a.id) is None
    assert mgr.get(b.id) is b
    assert mgr.get(c.id) is c


def test_created_session_carries_history_cap():
    mgr = _manager(FakeClock(), max_messages=2)
    session, _ = mgr.resume_or_create(None)
    assert session.conversation.max_messages == 2
