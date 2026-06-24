"""Streaming sentence splitter — the performance-critical seam.

It sits between the LLM token stream and TTS. Feeding it text incrementally lets
the first sentence reach TTS (and the client's speaker) before the full response
has been generated. Keep this boundary intact when modifying the pipeline.

Usage::

    splitter = SentenceSplitter()
    for token in llm_stream:
        for sentence in splitter.feed(token):
            yield sentence
    for sentence in splitter.flush():   # whatever's left after the stream ends
        yield sentence

The splitter is deliberately simple and dependency-free: it emits a sentence once
it sees terminal punctuation (``. ! ?``) followed by whitespace or end of buffer,
while avoiding obvious false splits on common abbreviations and decimals.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

_TERMINATORS = ".!?"
# Closing quotes/brackets that may trail a terminator and still end the sentence.
_CLOSERS = "\"')]}”’"
# Lowercased tokens that end in '.' but rarely end a sentence.
_ABBREVIATIONS = frozenset(
    {
        "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
        "vs.", "etc.", "e.g.", "i.e.", "a.m.", "p.m.", "u.s.", "u.k.",
        "no.", "fig.", "approx.", "dept.", "gen.", "gov.", "inc.", "ltd.",
    }
)


class SentenceSplitter:
    """Accumulates streamed text and yields complete sentences as they form."""

    def __init__(self) -> None:
        self._buf: str = ""

    def feed(self, text: str) -> Iterator[str]:
        """Add a chunk of text; yield any sentences that are now complete."""
        if not text:
            return
        self._buf += text
        yield from self._drain()

    def flush(self) -> Iterator[str]:
        """Yield any remaining buffered text as a final sentence."""
        remaining = self._buf.strip()
        self._buf = ""
        if remaining:
            yield remaining

    # --- internals ---

    def _drain(self) -> Iterator[str]:
        while True:
            cut = self._find_boundary()
            if cut is None:
                return
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if sentence:
                yield sentence

    def _find_boundary(self) -> int | None:
        """Return the index *after* the first sentence end, or None if incomplete.

        Two things end a spoken unit:

        * a newline — a hard break. The brain injects one when the model stops
          speaking to call a tool, so the spoken preamble ("let me check that")
          reaches TTS *before* the tool, possibly seconds, runs instead of being
          held back until after it returns.
        * a terminator (``. ! ?``), optionally trailed by closing quotes, that is
          followed by whitespace. We require the trailing whitespace so we never
          split a sentence whose final punctuation is still mid-stream (e.g. the
          "3." of "3.14").
        """
        for i, ch in enumerate(self._buf):
            if ch == "\n":
                return i + 1
            if ch not in _TERMINATORS:
                continue
            end = i + 1
            # absorb any closing quotes/brackets
            while end < len(self._buf) and self._buf[end] in _CLOSERS:
                end += 1
            # need whitespace after to confirm the sentence is finished
            if end >= len(self._buf) or not self._buf[end].isspace():
                continue
            if self._is_false_split(i, end):
                continue
            return end
        return None

    def _is_false_split(self, term_index: int, end: int) -> bool:
        # Decimal like "3.14": digit, dot, digit.
        if (
            self._buf[term_index] == "."
            and term_index > 0
            and self._buf[term_index - 1].isdigit()
            and end < len(self._buf)
        ):
            # whitespace already confirmed, so a following digit can't occur here;
            # this guards the "3.14" case where there's no trailing space at all,
            # handled by the whitespace check, but kept for clarity.
            pass
        # Known abbreviation immediately preceding the terminator.
        word = self._trailing_word(term_index + 1)
        if word.lower() in _ABBREVIATIONS:
            return True
        return False

    def _trailing_word(self, end: int) -> str:
        start = end
        while start > 0 and not self._buf[start - 1].isspace():
            start -= 1
        return self._buf[start:end]


def split_complete(text: str) -> list[str]:
    """Split a fully-formed string into sentences (convenience for non-streaming)."""
    splitter = SentenceSplitter()
    out: list[str] = list(splitter.feed(text))
    out.extend(splitter.flush())
    return out


def stream_sentences(chunks: Iterable[str]) -> Iterator[str]:
    """Run an iterable of text chunks through the splitter, yielding sentences."""
    splitter = SentenceSplitter()
    for chunk in chunks:
        yield from splitter.feed(chunk)
    yield from splitter.flush()
