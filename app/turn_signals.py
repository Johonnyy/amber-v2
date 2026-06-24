"""Per-turn back-channel from the brain to the pipeline (turn-based conversations).

The brain's contract with the pipeline is a stream of spoken text
(``AsyncIterator[str]``) — it has no structured way to say anything *about* the
turn. ``TurnSignals`` is that side channel: the pipeline makes one instance per
turn and threads it into :func:`app.brain.think` (the same explicit way
``client_tools`` is threaded), and the brain mutates it when the model invokes a
signaling tool.

Today there's one signal, ``awaiting_response``: set when the model calls the
``expect_reply`` tool to say it asked something it expects the user to answer, so
the pipeline can tell the client (via ``turn_complete``) to keep the mic open for
a continuation. Spoken text stays on the stream; turn-level facts live here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TurnSignals:
    """Mutable per-turn flags the brain sets and the pipeline reads.

    ``awaiting_response`` — Amber asked something it genuinely expects the user to
    answer (the model called ``expect_reply``); the client should keep listening
    and the next utterance is the continuation. Defaults to ``False`` so any turn
    that never signals (the common case, and the canned/fallback path) ends
    normally.
    """

    awaiting_response: bool = False
