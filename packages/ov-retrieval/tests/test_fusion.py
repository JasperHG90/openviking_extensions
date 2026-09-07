"""Unit tests for reciprocal rank fusion. No database, no OpenViking."""

from __future__ import annotations

import pytest

from ov_retrieval.fusion import RRF_K, rrf_fuse


def test_agreement_between_rankers_beats_a_single_strong_showing() -> None:
    """The whole point of fusing: consensus outranks one ranker's favourite."""
    fused = rrf_fuse(
        {
            "vector": ["a", "b", "c"],
            "keyword": ["b", "a", "d"],
        }
    )

    assert [key for key, _ in fused][:2] == ["a", "b"]
    # "c" is third for one ranker and absent from the other; "d" the mirror.
    # Neither should reach the top.
    assert set(key for key, _ in fused) == {"a", "b", "c", "d"}


def test_only_positions_matter_not_scores() -> None:
    """A ranker can be retuned without recalibrating the fusion.

    This is why RRF is the right choice for mixing a cosine distance with a
    ts_rank_cd value: the two are not on a common scale, and never become one.
    """
    first = rrf_fuse({"a": ["x", "y", "z"], "b": ["y", "x", "z"]})
    same_order_again = rrf_fuse({"a": ["x", "y", "z"], "b": ["y", "x", "z"]})

    assert first == same_order_again


def test_an_item_absent_from_a_ranker_simply_scores_nothing_there() -> None:
    """A keyword search that cannot match a document must not be made to."""
    fused = dict(rrf_fuse({"vector": ["a"], "keyword": ["b"]}))

    assert fused["a"] == pytest.approx(1 / (RRF_K + 1))
    assert fused["b"] == pytest.approx(1 / (RRF_K + 1))


def test_weights_scale_a_ranker_s_whole_contribution() -> None:
    """Halving a ranker's weight halves every contribution it makes."""
    full = dict(rrf_fuse({"vector": ["a"], "keyword": ["b"]}))
    halved = dict(rrf_fuse({"vector": ["a"], "keyword": ["b"]}, weights={"keyword": 0.5}))

    assert halved["a"] == pytest.approx(full["a"])
    assert halved["b"] == pytest.approx(full["b"] * 0.5)


def test_zero_weight_drops_a_ranker_without_touching_the_call_site() -> None:
    """Lets a caller disable the keyword leg by config alone."""
    fused = rrf_fuse({"vector": ["a"], "keyword": ["b"]}, weights={"keyword": 0.0})

    assert [key for key, _ in fused] == ["a"]


def test_a_repeated_key_counts_once_at_its_best_position() -> None:
    """A ranker returning a duplicate must not double-vote for it."""
    once = dict(rrf_fuse({"r": ["a", "b"]}))
    twice = dict(rrf_fuse({"r": ["a", "b", "a"]}))

    assert twice["a"] == pytest.approx(once["a"])


def test_ties_break_on_best_position_not_dict_order() -> None:
    """Two items with equal fused scores still need a stable order."""
    forwards = rrf_fuse({"one": ["a"], "two": ["b"]})
    backwards = rrf_fuse({"two": ["b"], "one": ["a"]})

    assert [key for key, _ in forwards] == [key for key, _ in backwards]


def test_limit_truncates_after_scoring_not_before() -> None:
    """Truncating first would drop an item that fusion would have promoted."""
    fused = rrf_fuse({"a": ["x", "y", "z"], "b": ["z", "y", "x"]}, limit=1)

    assert len(fused) == 1


def test_empty_input_is_not_an_error() -> None:
    """A query where every leg returned nothing is ordinary, not exceptional."""
    assert rrf_fuse({}) == []
    assert rrf_fuse({"vector": []}) == []


def test_smaller_k_sharpens_the_preference_for_first_place() -> None:
    """k controls how much rank 1 is worth over rank 2."""
    sharp = dict(rrf_fuse({"r": ["a", "b"]}, k=1))
    smooth = dict(rrf_fuse({"r": ["a", "b"]}, k=1000))

    assert sharp["a"] / sharp["b"] > smooth["a"] / smooth["b"]


def test_negative_k_is_refused() -> None:
    """k = -1 would divide by zero at rank 1; more negative inverts the sign."""
    with pytest.raises(ValueError, match="must not be negative"):
        rrf_fuse({"r": ["a"]}, k=-1)
