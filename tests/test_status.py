"""What the status frame says, and the one thing it must never say.

The leak test is the important one. `PeerRecord` carries the peer's bearer token, and
this module's whole shape — take an object, turn it into a dict for the wire — is the
shape that ships one to every connected client. `as_dict()` is right there and does
exactly the wrong thing.
"""

import pytest

from app import protocol, status as status_report
from app.config import Settings
from app.session import Session, get_session_manager


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


@pytest.fixture
def session() -> Session:
    # Through the manager rather than the constructor: `Session` takes a conversation,
    # two timestamps and a limiter, and a test that hand-rolls them would quietly stop
    # representing a real session the moment one of those changes.
    session, _ = get_session_manager().resume_or_create(None)
    return session


async def test_the_frame_describes_this_install(session):
    frame = protocol.status(**await status_report.build(session, _settings()))

    assert frame["type"] == protocol.STATUS
    assert frame["session"]["id"] == session.id
    assert frame["session"]["max_turns"] == _settings().max_turns_per_session
    assert "peers" in frame
    assert "features" in frame
    assert "memory" in frame


async def test_no_peer_token_reaches_the_wire(session, monkeypatch):
    """The failure this module is shaped to prevent."""
    secret = "super-secret-bearer-token"

    class FakeRecord:
        base_url = "https://bloom.example"
        version = "1.2.3"
        tools = ("run_task", "build_agent")
        token = secret

        def as_dict(self):  # the convenience that would leak it
            return {"base_url": self.base_url, "token": self.token}

    class FakeRegistry:
        def resolve(self, name):
            return FakeRecord()

    monkeypatch.setattr(status_report.peer_discovery, "known_peers", lambda s: ["bloom"])
    monkeypatch.setattr(status_report.peer_discovery, "registry", lambda: FakeRegistry())

    frame = protocol.status(**await status_report.build(session, _settings()))

    assert secret not in repr(frame)
    peer = frame["peers"]["known"][0]
    assert peer["name"] == "bloom"
    assert peer["base_url"] == "https://bloom.example"
    assert peer["tools"] == 2
    assert "token" not in peer


async def test_a_peer_that_will_not_resolve_is_still_listed(session, monkeypatch):
    """Knowing Amber thinks she can reach something is the useful half."""

    class Broken:
        def resolve(self, name):
            raise KeyError(name)

    monkeypatch.setattr(status_report.peer_discovery, "known_peers", lambda s: ["ghost"])
    monkeypatch.setattr(status_report.peer_discovery, "registry", lambda: Broken())

    frame = protocol.status(**await status_report.build(session, _settings()))

    assert frame["peers"]["known"] == [{"name": "ghost"}]


async def test_a_failing_section_costs_only_that_section(session, monkeypatch):
    """A status frame is a nicety; it must never take a connection down with it."""
    monkeypatch.setattr(
        status_report.peer_discovery,
        "status",
        lambda s: (_ for _ in ()).throw(RuntimeError("registry on fire")),
    )

    sections = await status_report.build(session, _settings())

    assert "peers" not in sections
    assert "session" in sections  # everything else still arrived


async def test_features_report_what_is_actually_on(session):
    sections = await status_report.build(
        session, _settings(feature_memory=False, feature_tools=False)
    )

    assert sections["features"]["memory"] is False
    assert sections["features"]["tools"] is False
    # And with memory off there is nothing to count rather than an error.
    assert sections["memory"]["facts"] == 0
