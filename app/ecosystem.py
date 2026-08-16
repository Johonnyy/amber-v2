"""What Amber knows about the system she's part of.

Amber is not a general assistant that happens to run on a server — she is one
component of a personal ecosystem (Aperture, the shared libraries, the sync store,
Bloom, the domain apps), and she is the natural-language way into all of it. Without
this block she answers "what is Bloom?" or "how do you work?" by guessing from her
training data or reaching for a web search, both of which are wrong: the answer is
private and she is standing inside it.

Two rules kept this from becoming the kind of prompt bloat `app.persona` was written
to end:

* **Framework knowledge, not documentation.** Each piece gets one line saying what it
  *is for*. No file paths, no config keys, no endpoint names — none of that survives
  being spoken aloud, and all of it rots the moment the code moves. If Johnny wants
  detail he is asking the agent that has the repo open, not the one in his ear.
* **Claims are checked against this install, not against the plan.** The static half
  describes the shape of the ecosystem; `_wiring_block` names only what *this* process
  can actually reach — real peers by their real names, the sync store when one is
  configured. The prompt closes by telling her that a missing tool means a missing
  capability, because the failure mode of handing a model an architecture diagram is
  that it starts describing planned features in the present tense.

  That failure happened anyway, and both halves of why are worth keeping written
  down. The closing guard named only "the domain apps", so it did not cover Bloom —
  which is the one piece the overview describes as the thing work gets handed to. And
  the wiring block said *nothing* when there were no peers, so the claim above it
  stood unopposed. Asked what she could reach, Amber answered "I can hand work off to
  the bloom agent"; asked to actually build something there, she had no tool, fell
  back to her training data, and produced `npm install` instructions for a service
  that is Python and takes no code at all. She was reading her prompt correctly.
  A negative is now emitted explicitly, and the peer list comes from
  `app.peers.known_peers` — the same call `app.brain.build_broker` makes — so what
  she is told she can reach and what she is actually handed cannot drift apart.

Gated by ``AMBER_FEATURE_ECOSYSTEM_CONTEXT``: every turn pays for these tokens, and a
deployment that only wants a voice assistant should be able to stop paying for them.
"""

from __future__ import annotations

from app import peers as peer_discovery
from app.config import Settings, get_settings

# The shape of the ecosystem. Deliberately written the way she'd say it out loud —
# no paths, no env vars, no URLs (the spoken-style rules forbid reading those anyway).
_OVERVIEW = """\
What you're part of

You're one piece of a personal system Johnny built, and you know how it fits
together — so answer questions about it directly instead of guessing or searching.

- You: a Python service on his server with no interface of your own. Clients connect
  over a socket and send speech or typed text; you transcribe, think, and stream the
  reply back sentence by sentence as audio. Thinking is a loop — call a tool, read
  the result, keep going until you can answer. Your memory is distilled facts in a
  small database, tiered by how durable they've proven and tidied on a timer — not a
  transcript of what was said.
- Aperture: the desktop app most conversations reach you through. It's the visual
  front end for everything here — the rule there is to show things rather than
  describe them — and it holds Johnny's keys, so it can act on his machines.
- agent-runtime: the shared library your thinking loop lives in. agent-mcp-py: the
  one every other app uses to expose its data and its actions to you. Between them
  they're why you can reach anything else at all.
- amber-infra: the deployment side — the installer, the backups, the edge that gives
  everything its own address, and the sync store, which holds the registry of what's
  running where, Aperture's settings, and the shared list of model keywords that
  makes a word like "coding" mean the same model in every app.
- Bloom: where a new agent gets defined rather than built — a prompt, a model
  keyword, and the accounts it's allowed to act through. Filling in a form, not
  writing code: nothing to clone, install or pick a library for. It's what
  specialised work gets handed to instead of teaching you another integration.

The domain apps — finance, school, the rest — are planned, not built. Knowing what a
piece of this is FOR is not the same as being able to reach it, Bloom included: the
list below is what you can actually reach, and anything not on it has no tool and
isn't wired up yet. Say that in a few words rather than guessing at how it might
otherwise be done."""


def _wiring_block(settings: Settings) -> str | None:
    """Name what this install can actually reach, or ``None`` if nothing.

    The overview above describes the design; this describes the deployment. They are
    separate because the design is stable and the deployment isn't — and because a
    peer named here is a peer the broker really loaded, read from the same parser
    `app.brain` uses rather than a second list that could disagree with it.
    """
    lines: list[str] = []

    peers = peer_discovery.known_peers(settings)
    if peers:
        listed = ", ".join(peers)
        # No note about filler lines here: the core prompt already covers "handing
        # work to a peer" under slow tools, and saying it twice is how a prompt
        # starts contradicting itself.
        lines.append(f"Other agents you can hand work to, by name: {listed}.")
    else:
        # The absence, said out loud. This used to emit nothing at all, and nothing
        # is not neutral: the overview above describes Bloom as the thing specialised
        # work gets handed to, so silence here left that claim standing unopposed.
        # Amber duly reported she could "hand work off to the bloom agent" while
        # holding no tool for it — and then, asked to actually use it, fell back to
        # her training data and produced npm commands for a Python service.
        #
        # A negative is information. It costs one line and it is the only thing on
        # this surface that can contradict the paragraph above it.
        # One sentence, and no advice about what to say instead — the overview above
        # already ends with that instruction, and this module's own rule is that
        # saying a thing twice is how a prompt starts contradicting itself.
        lines.append(
            "No other agents are reachable, Bloom included, so work that would need "
            "one can't be handed off yet."
        )
    if settings.mcp_sync_store_url:
        lines.append(
            "You're registered in the sync store, so the rest of the system can find "
            "you without being told where you are."
        )
    if settings.mcp_server_enabled:
        lines.append(
            "It works both ways: other agents can query you, and what they see is "
            "your memory, your tasks and your reminders."
        )

    if not lines:
        return None
    return "Right now:\n" + "\n".join(f"- {line}" for line in lines)


def build_ecosystem_block(settings: Settings | None = None) -> str | None:
    """The ecosystem block for this turn, or ``None`` when the flag is off.

    Pure and cheap — string joins over settings, no I/O — so the pipeline can rebuild
    it per turn and pick up a config change without a restart, the same way the
    runtime-context stamp is rebuilt.
    """
    settings = settings or get_settings()
    if not settings.feature_ecosystem_context:
        return None

    wiring = _wiring_block(settings)
    return _OVERVIEW if wiring is None else f"{_OVERVIEW}\n\n{wiring}"
