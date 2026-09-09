"""Properties of the query clip the keyword leg applies.

Its own module rather than part of ``test_retriever``: these tests are sync,
and that module's asyncio ``pytestmark`` would land on them and make
pytest-asyncio warn -- the same trap ``test_observability`` documents.
"""

from __future__ import annotations

import pytest

from ov_ext.retrieval.retriever import _clip_query

QUERIES = [
    "",
    "   ",
    "one",
    "a bb ccc dddd",
    "tabs\tand\nnewlines\tcount as boundaries too",
    "onegiantwordwithnobreaksatall",
    "  leading space then words",
]


@pytest.mark.parametrize("text", QUERIES)
@pytest.mark.parametrize("max_chars", [0, 1, 2, 5, 12, 1024])
def test_the_clip_always_returns_a_bounded_prefix(text: str, max_chars: int) -> None:
    """Two properties the keyword leg leans on, over the shapes a query takes.

    A result that is not a prefix would search for words in an order nobody
    wrote, and one over the ceiling would defeat the clip.
    """
    clipped = _clip_query(text, max_chars)

    assert text.startswith(clipped)
    assert max_chars <= 0 or len(clipped) <= max_chars


@pytest.mark.parametrize("text", QUERIES)
def test_a_ceiling_of_zero_disables_the_clip(text: str) -> None:
    """Zero means send it all, the way the rerank ceilings read zero."""
    assert _clip_query(text, 0) == text


def test_the_clip_ends_on_a_whole_word() -> None:
    """A fragment stems to a lexeme of its own and matches rows nobody asked for."""
    assert _clip_query("alpha beta gamma", 8) == "alpha"


def test_a_clean_boundary_keeps_the_whole_word_before_it() -> None:
    """Cutting where a word already ends must not also drop that word."""
    assert _clip_query("alpha beta gamma", 11) == "alpha beta"
    assert _clip_query("alpha beta gamma", 10) == "alpha beta"


def test_a_word_longer_than_the_ceiling_is_cut_mid_word() -> None:
    """No boundary to fall back to, and returning nothing would search nothing."""
    assert _clip_query("onegiantword", 4) == "oneg"


@pytest.mark.parametrize("max_chars", [4, 5, 6, 7, 8])
def test_a_cut_inside_a_run_of_spaces_ends_on_the_word(max_chars: int) -> None:
    """Both branches strip, so the result ends where a word ends whichever ran.

    4 to 6 land in the spaces themselves, 7 and 8 land in ``def`` and fall
    back to the boundary before it. ``"abc   def"`` is nine characters, so a
    ceiling of nine or more returns it whole and clips nothing.
    """
    assert _clip_query("abc   def", max_chars) == "abc"
