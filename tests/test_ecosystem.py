"""Tests for the ecosystem block.

Like the persona itself, this is text with no compiler behind it — the failure mode
isn't an exception, it's Amber confidently describing a peer she can't reach or a
domain app that was never built. These tests pin the two properties that keep that
from happening: the block never claims wiring this install doesn't have, and it stays
small enough to pay for on every single turn.
"""

import pytest

from app.config import Settings
from app.ecosystem import build_ecosystem_block
from app.persona import compose_system_prompt


def _settings(**overrides) -> Settings:
    """Settings built from defaults only, ignoring any `.env` on the machine."""
    return Settings(_env_file=None, **overrides)


# --- the flag ---

def test_the_flag_turns_the_whole_block_off():
    assert build_ecosystem_block(_settings(feature_ecosystem_context=False)) is None


def test_it_is_on_by_default():
    """She is the natural-language way into the system; not knowing what the system
    is makes her search the web for something private."""
    assert build_ecosystem_block(_settings()) is not None


# --- what it says ---

@pytest.mark.parametrize(
    "piece", ["Aperture", "agent-runtime", "agent-mcp-py", "amber-infra", "Bloom"]
)
def test_every_piece_of_the_ecosystem_is_named(piece):
    assert piece in build_ecosystem_block(_settings())


def test_it_says_a_missing_tool_means_a_missing_capability():
    """The failure mode of handing a model an architecture diagram is that it starts
    describing planned features in the present tense."""
    block = build_ecosystem_block(_settings())
    assert "planned, not built" in block
    # Matched in pieces because the source is hard-wrapped and a phrase that spans a
    # line break contains a newline the assertion would have to know about.
    assert "has no tool and" in block
    assert "isn't wired up yet" in block
    # The guard used to name only the domain apps, which left the one piece the
    # overview calls the delegation target outside its scope — and that is exactly
    # the capability Amber went on to claim she had.
    assert "Bloom included" in block


def test_it_carries_no_paths_keys_or_urls():
    """None of it can be spoken, and all of it rots when the code moves."""
    block = build_ecosystem_block(_settings())
    for marker in ("http", "AMBER_", "app/", ".py", "/mcp"):
        assert marker not in block, f"{marker!r} leaked into the ecosystem block"


# --- what this install can actually reach ---

def test_having_no_peers_is_stated_rather_than_left_silent():
    """The bug this replaces, and it cost a whole exchange.

    This used to assert the wiring section was ABSENT on a bare install, on the
    theory that claiming nothing is the safe default. It isn't: the overview above
    describes Bloom as the thing specialised work gets handed to, so saying nothing
    leaves that standing. Amber duly reported she could "hand work off to the bloom
    agent" while holding no tool for it, then invented npm commands when asked to
    use it. Silence is not neutral when the paragraph above it makes a claim.
    """
    block = build_ecosystem_block(_settings(feature_mcp_server=False))
    assert "No other agents are reachable" in block


def test_configured_peers_are_named():
    block = build_ecosystem_block(
        _settings(mcp_peers="bloom=https://bloom.example,finance=https://fin.example")
    )
    assert "bloom" in block
    assert "finance" in block
    assert "No other agents are reachable" not in block


def test_a_malformed_peer_entry_is_not_announced():
    """Peers come from the same parser the broker uses, so what's named here is what
    was really loaded — a typo drops the entry in both places, never just one."""
    block = build_ecosystem_block(_settings(mcp_peers="justaname,"))
    assert "justaname" not in block


def test_the_prompt_and_the_broker_agree_about_which_peers_exist():
    """The invariant, asserted rather than trusted.

    These are two readers of one list and they drifted the moment `build_broker`
    learned about discovery and this file did not. A model cannot tell a capability
    it has from one its prompt asserts, so the two must be computed by the same call.
    """
    from app import brain, peers

    settings = _settings(
        feature_tools=True,
        mcp_peers="bloom=https://bloom.example",
        openrouter_api_key="sk-test",
        memory_db_path=":memory:",
    )
    names = peers.known_peers(settings)
    block = build_ecosystem_block(settings)
    broker = brain.build_broker(settings)
    client = broker.brokers[-1]

    assert names == ["bloom"]
    assert client.servers == names
    for name in names:
        assert name in block


def test_a_discovered_peer_reaches_the_prompt_too():
    """The half that was still broken after the broker was fixed: she would have had
    a bloom__* tool and a prompt that never mentioned Bloom."""
    from agent_mcp.registry import PeerRecord, default_registry

    from app import peers

    reg = default_registry()
    static, discovered = dict(reg._static), dict(reg._discovered)
    try:
        reg._discovered = {"bloom": PeerRecord(name="bloom", base_url="https://b.test")}
        block = build_ecosystem_block(_settings(mcp_peers=""))
        assert "bloom" in block
        assert "No other agents are reachable" not in block
        assert peers.known_peers(_settings(mcp_peers="")) == ["bloom"]
    finally:
        reg._static, reg._discovered = static, discovered


def test_the_sync_store_is_only_mentioned_when_one_is_configured():
    without = build_ecosystem_block(_settings())
    with_store = build_ecosystem_block(
        _settings(mcp_sync_store_url="https://sync.example")
    )
    assert "sync store, so" not in without
    assert "sync store, so" in with_store


def test_being_queryable_needs_the_server_to_actually_mount():
    """`mcp_server_enabled` requires keys as well as the flag — agent-mcp-py fails
    closed, so the flag alone means no server and nothing to advertise."""
    flag_only = build_ecosystem_block(_settings(feature_mcp_server=True, mcp_keys=""))
    mounted = build_ecosystem_block(
        _settings(feature_mcp_server=True, mcp_keys="aperture:secret")
    )
    assert "other agents can query you" not in flag_only.lower()
    assert "other agents can query you" in mounted.lower()


# --- cost ---

#: Paid on every turn, on top of the persona and the memory block, so growth is a
#: deliberate decision rather than a drift.
#:
#: Raised from 2200 once, and what bought it is worth recording: the closing guard
#: ("knowing what a piece of this is FOR is not the same as being able to reach it")
#: had named only the domain apps, so it did not cover the one piece the overview
#: describes as the delegation target — and the wiring section said nothing at all
#: when there were no peers, leaving that claim unopposed. Amber reported she could
#: hand work to Bloom while holding no tool for it, then produced npm instructions
#: for a Python service. The extra ~200 characters are the sentences that stop that.
ECOSYSTEM_BLOCK_CEILING = 2500


def test_the_block_stays_within_its_token_budget():
    """Measured on the LARGEST form, which is the one with no peers.

    It used to be measured with a peer configured, on the reasonable-looking theory
    that more wiring means more text. That stopped being true the moment absence
    started being stated out loud: naming one peer is shorter than explaining that
    there are none. A budget test that measures a convenient case is not a budget.
    """
    forms = {
        "peer + store + server": _settings(
            mcp_peers="bloom=https://bloom.example",
            mcp_sync_store_url="https://sync.example",
            mcp_keys="aperture:secret",
        ),
        "no peers": _settings(
            mcp_sync_store_url="https://sync.example",
            mcp_keys="aperture:secret",
        ),
        "bare install": _settings(feature_mcp_server=False),
    }
    sizes = {label: len(build_ecosystem_block(s)) for label, s in forms.items()}
    worst, size = max(sizes.items(), key=lambda kv: kv[1])
    assert size < ECOSYSTEM_BLOCK_CEILING, (
        f"ecosystem block grew to {size} chars on the {worst!r} form "
        f"(ceiling {ECOSYSTEM_BLOCK_CEILING}); trim something or move the ceiling "
        f"deliberately. All forms: {sizes}"
    )


# --- composition ---

def test_it_lands_with_identity_rather_than_with_per_turn_context():
    """It's background about who she is, not context about this turn."""
    prompt = compose_system_prompt(
        ecosystem_block="What you're part of: the system.",
        client_tool_names=["client_show_text"],
        runtime_context="Right now it's Tuesday.",
        memory_block="What you remember: nothing.",
    )
    order = [
        prompt.index("You are Amber"),
        prompt.index("What you're part of"),
        prompt.index("client_show_text"),
        prompt.index("Right now it's"),
        prompt.index("What you remember"),
    ]
    assert order == sorted(order)


def test_the_prompt_is_unchanged_without_it():
    assert compose_system_prompt(ecosystem_block=None) == compose_system_prompt()
