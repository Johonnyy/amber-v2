"""Tests for the streaming sentence splitter — the performance-critical seam."""

from app.sentence_splitter import (
    SentenceSplitter,
    split_complete,
    stream_sentences,
)


def test_splits_on_terminators():
    assert split_complete("Hello there. How are you? Great!") == [
        "Hello there.",
        "How are you?",
        "Great!",
    ]


def test_trailing_fragment_emitted_on_flush():
    s = SentenceSplitter()
    assert list(s.feed("No terminator yet")) == []
    assert list(s.flush()) == ["No terminator yet"]


def test_first_sentence_emitted_before_stream_ends():
    """The whole point: sentence 1 is available mid-stream, not at the end."""
    s = SentenceSplitter()
    emitted = []
    for tok in ["Hel", "lo ", "world. ", "More ", "text"]:
        emitted.extend(s.feed(tok))
    # "Hello world." came out before we ever called flush()
    assert emitted == ["Hello world."]
    assert list(s.flush()) == ["More text"]


def test_terminator_split_across_chunks():
    s = SentenceSplitter()
    out = []
    out.extend(s.feed("Wait"))
    out.extend(s.feed("."))      # terminator arrives, but no trailing space yet
    assert out == []             # don't split until we know it's sentence-final
    out.extend(s.feed(" Next"))  # now whitespace confirms the boundary
    assert out == ["Wait."]


def test_decimals_not_split():
    assert split_complete("Pi is 3.14 today. Done.") == [
        "Pi is 3.14 today.",
        "Done.",
    ]


def test_abbreviations_not_split():
    assert split_complete("Dr. Smith arrived. He waved.") == [
        "Dr. Smith arrived.",
        "He waved.",
    ]


def test_closing_quote_absorbed():
    assert split_complete('She said "hello." Then left.') == [
        'She said "hello."',
        "Then left.",
    ]


def test_stream_sentences_helper():
    chunks = ["One. ", "Two. ", "Three."]
    assert list(stream_sentences(chunks)) == ["One.", "Two.", "Three."]


def test_empty_input_is_safe():
    s = SentenceSplitter()
    assert list(s.feed("")) == []
    assert list(s.flush()) == []
