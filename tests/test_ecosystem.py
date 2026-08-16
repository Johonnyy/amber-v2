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
    assert "no tool for it" in block


def test_it_carries_no_paths_keys_or_urls():
    """None of it can be spoken, and all of it rots when the code moves."""
    block = build_ecosystem_block(_settings())
    for marker in ("http", "AMBER_", "app/", ".py", "/mcp"):
        assert marker not in block, f"{marker!r} leaked into the ecosystem block"


# --- what this install can actually reach ---

def test_no_wiring_section_when_nothing_is_configured():
    """A bare install has no peers, no store, no server of its own — and should claim
    none of them."""
    block = build_ecosystem_block(_settings(feature_mcp_server=False))
    assert "Right now:" not in block


def test_configured_peers_are_named():
    block = build_ecosystem_block(
        _settings(mcp_peers="bloom=https://bloom.example,finance=https://fin.example")
    )
    assert "bloom" in block
    assert "finance" in block


def test_a_malformed_peer_entry_is_not_announced():
    """Peers come from the same parser the broker uses, so what's named here is what
    was really loaded — a typo drops the entry in both places, never just one."""
    block = build_ecosystem_block(_settings(mcp_peers="justaname,"))
    assert "justaname" not in block


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

def test_the_block_stays_within_its_token_budget():
    """Paid on every turn, on top of the persona and the memory block. Growth should
    be a deliberate decision — if this fails, trim something or move the ceiling."""
    block = build_ecosystem_block(
        _settings(
            mcp_peers="bloom=https://bloom.example",
            mcp_sync_store_url="https://sync.example",
            mcp_keys="aperture:secret",
        )
    )
    assert len(block) < 2200, f"ecosystem block grew to {len(block)} chars"


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
