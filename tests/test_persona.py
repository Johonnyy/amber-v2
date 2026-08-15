"""Tests for the composed system prompt.

The prompt is the one part of Amber with no compiler and no runtime error to catch
it when it goes wrong — it just quietly gets worse. The previous single prompt drifted
badly: it described a backend that had been deleted, banned emoji in one paragraph
and required one in the next, and gave "spell it for the ear" rules to typed turns.
These tests pin the properties that drift violates.
"""

import re

from app.persona import SYSTEM_PROMPT, compose_system_prompt


def test_the_prompt_opens_by_saying_who_she_is():
    assert SYSTEM_PROMPT.startswith("You are Amber")


def test_the_bare_prompt_needs_no_per_turn_context():
    """`app.brain` falls back to this when a caller streams without composing one."""
    assert SYSTEM_PROMPT == compose_system_prompt()
    assert "Right now it's" not in SYSTEM_PROMPT


# --- the drift that prompted the rewrite ---

def test_no_reference_to_the_removed_backend():
    """OpenClaw was deleted; delegation goes to peer MCP servers now. The prompt
    named it three times, so the model was being told about a tool it didn't have."""
    prompt = compose_system_prompt(client_tool_names=["client_show_text"])
    assert "openclaw" not in prompt.lower()


def test_the_prompt_does_not_contradict_itself_about_emoji():
    """It used to ban emoji in the style rules and require a flag emoji nine lines
    later."""
    assert "emoji" in SYSTEM_PROMPT
    assert "flag emoji" not in SYSTEM_PROMPT.lower()


def test_no_display_tool_guidance_when_the_device_declared_none():
    """Nine lines describing cards, tables, weather tiles and maps were injected on
    every turn, including to a headless earpiece that had none of them."""
    prompt = compose_system_prompt()
    assert "screen" not in prompt.lower()


# --- modality ---

def test_a_spoken_turn_gets_the_rules_for_the_ear():
    prompt = compose_system_prompt(modality="voice")
    assert "thirty dollars" in prompt
    assert "Never read out a URL" in prompt


def test_a_typed_turn_does_not_get_them():
    """Spelling "$30" out phonetically makes no sense on a screen."""
    prompt = compose_system_prompt(modality="text")
    assert "thirty dollars" not in prompt
    assert "typed" in prompt


def test_both_modalities_still_ban_markdown():
    """Every reply is spoken, however it arrived — so formatting is never useful."""
    for modality in ("voice", "text"):
        assert "markdown" in compose_system_prompt(modality=modality).lower()


def test_an_unknown_modality_falls_back_to_speech():
    assert compose_system_prompt(modality="carrier pigeon") == compose_system_prompt()


# --- the device block ---

def test_declared_client_tools_are_named_in_the_prompt():
    """Naming what was actually declared is what stops this drifting: the previous
    hard-coded paragraph could describe tools the device never had."""
    prompt = compose_system_prompt(
        client_tool_names=["client_show_card", "client_play_sound"]
    )
    assert "client_show_card" in prompt
    assert "client_play_sound" in prompt
    assert "screen" in prompt


def test_empty_tool_names_are_ignored():
    assert compose_system_prompt(client_tool_names=["", None]) == compose_system_prompt()


# --- per-turn context blocks ---

def test_context_blocks_are_appended_in_narrowing_order():
    prompt = compose_system_prompt(
        runtime_context="Right now it's Tuesday.",
        memory_block="What you remember: nothing.",
        client_tool_names=["client_show_text"],
        notes_block="Patterns you've noticed: none.",
    )
    order = [
        prompt.index("You are Amber"),
        prompt.index("client_show_text"),
        prompt.index("Patterns you've noticed"),
        prompt.index("Right now it's"),
        prompt.index("What you remember"),
    ]
    assert order == sorted(order)


def test_blocks_are_omitted_when_absent():
    prompt = compose_system_prompt(runtime_context=None, memory_block=None)
    assert prompt == compose_system_prompt()


# --- what the prompt has to say ---

def test_it_tells_her_to_look_things_up_rather_than_answer_from_stale_memory():
    """The single biggest quality problem: confidently out-of-date answers."""
    lowered = SYSTEM_PROMPT.lower()
    assert "cutoff" in lowered
    assert "look it up" in lowered or "search" in lowered


def test_it_tells_her_to_keep_answers_short():
    assert "One sentence by default" in SYSTEM_PROMPT
    assert "Lead with the answer" in SYSTEM_PROMPT


def test_it_tells_her_never_to_say_a_fact_id_aloud():
    """Ids are in the memory block so tools can act on a fact; they are not speech."""
    assert "never say an id out loud" in SYSTEM_PROMPT


def test_it_keeps_the_filler_before_slow_tools():
    """The one place Amber announces a tool — it's what keeps a multi-second search
    from sounding like a dropped call."""
    assert "one sec" in SYSTEM_PROMPT.lower()


def test_the_prompt_stays_within_a_sane_token_budget():
    """Every turn pays for this. Growth should be a deliberate decision, not a
    drift — if this fails, either trim something or move the ceiling on purpose."""
    prompt = compose_system_prompt(
        modality="voice", client_tool_names=["client_show_card"]
    )
    assert len(prompt) < 5000, f"system prompt grew to {len(prompt)} chars"


def test_the_prompt_has_no_leftover_formatting_markers():
    """It's read by a model, not rendered — stray markdown headings in the prompt
    invite markdown in the reply, which cannot be spoken."""
    assert not re.search(r"^#{1,6} ", SYSTEM_PROMPT, re.MULTILINE)
    assert "```" not in SYSTEM_PROMPT
