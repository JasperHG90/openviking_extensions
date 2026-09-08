"""Unit tests for MMR selection and the similarity helpers it consumes."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ov_ext.retrieval.diversity import (
    blend_similarity,
    jaccard_similarity,
    mmr_select,
    tag_similarity_matrix,
)


@dataclass
class Doc:
    """A retrieval candidate, standing in for an OpenViking result."""

    uri: str
    tags: list[str] = field(default_factory=list)
    updated_at: float = 0.0


def sym(pairs: dict[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    """Mirror a half-matrix, as the real backends return it."""
    out: dict[tuple[str, str], float] = {}
    for (a, b), value in pairs.items():
        out[(a, b)] = value
        out[(b, a)] = value
    return out


def test_a_near_duplicate_is_pushed_below_a_novel_result() -> None:
    """The behaviour the whole module exists for."""
    docs = [Doc("a"), Doc("a-copy"), Doc("b")]
    similarity = sym({("a", "a-copy"): 0.99})

    order = [d.uri for d in mmr_select(docs, key=lambda d: d.uri, similarity=similarity)]

    assert order == ["a", "b", "a-copy"]


def test_the_top_result_is_always_kept() -> None:
    """It is the most relevant thing present and duplicates nothing yet."""
    docs = [Doc("a"), Doc("b")]
    similarity = sym({("a", "b"): 1.0})

    assert mmr_select(docs, key=lambda d: d.uri, similarity=similarity)[0].uri == "a"


def test_lambda_one_ignores_similarity_entirely() -> None:
    """Relevance-only, i.e. the ordering MMR was given."""
    docs = [Doc("a"), Doc("a-copy"), Doc("b")]
    similarity = sym({("a", "a-copy"): 0.99})

    order = [
        d.uri
        for d in mmr_select(docs, key=lambda d: d.uri, similarity=similarity, lambda_=1.0)
    ]

    assert order == ["a", "a-copy", "b"]


def test_lambda_zero_ranks_on_novelty_alone() -> None:
    """The far end of the trade-off still has to be well defined."""
    docs = [Doc("a"), Doc("a-copy"), Doc("b")]
    similarity = sym({("a", "a-copy"): 0.99})

    order = [
        d.uri
        for d in mmr_select(docs, key=lambda d: d.uri, similarity=similarity, lambda_=0.0)
    ]

    assert order[1] == "b", "the dissimilar document must come first"


def test_an_unknown_pair_counts_as_novel_not_as_suppressed() -> None:
    """A row with no embedding must not be silently buried."""
    docs = [Doc("a"), Doc("b"), Doc("c")]

    order = [d.uri for d in mmr_select(docs, key=lambda d: d.uri, similarity={})]

    assert order == ["a", "b", "c"]


def test_limit_stops_the_selection_early() -> None:
    """Selection is greedy, so the loop can stop as soon as it has enough."""
    docs = [Doc(x) for x in "abcde"]

    assert len(mmr_select(docs, key=lambda d: d.uri, similarity={}, limit=2)) == 2


def test_tiebreak_settles_a_near_tie() -> None:
    """Without it, floating-point noise would decide which of two twins wins.

    A tie has to be built deliberately, because relevance comes from position
    and so no two candidates ever share it. With three candidates and the
    default lambda, "old" sits one rank above "new" and is worth
    ``0.7 * (2/3 - 1/3) = 0.233`` more; a similarity of ``0.78`` to the
    already-selected "a" costs it ``0.3 * 0.78 = 0.234``, landing the two
    within the epsilon.
    """
    docs = [Doc("a"), Doc("old", updated_at=1.0), Doc("new", updated_at=99.0)]
    similarity = sym({("a", "old"): 0.78})

    without = [
        d.uri for d in mmr_select(docs, key=lambda d: d.uri, similarity=similarity)
    ]
    with_tiebreak = [
        d.uri
        for d in mmr_select(
            docs,
            key=lambda d: d.uri,
            similarity=similarity,
            tiebreak=lambda d: d.updated_at,
        )
    ]

    assert without[1] == "old", "the earlier candidate holds the slot by default"
    assert with_tiebreak[1] == "new", "the tiebreaker prefers the newer record"


def test_short_inputs_pass_straight_through() -> None:
    """Nothing to diversify against."""
    assert mmr_select([], key=lambda d: d.uri, similarity={}) == []
    one = [Doc("a")]
    assert mmr_select(one, key=lambda d: d.uri, similarity={}) == one


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_lambda_outside_the_unit_interval_is_refused(bad: float) -> None:
    """Outside 0..1 the formula stops expressing a trade-off."""
    with pytest.raises(ValueError, match="between 0 and 1"):
        mmr_select([Doc("a"), Doc("b")], key=lambda d: d.uri, similarity={}, lambda_=bad)


def test_every_candidate_is_returned_when_no_limit_is_given() -> None:
    """MMR re-orders; it does not drop."""
    docs = [Doc(x) for x in "abcde"]

    assert len(mmr_select(docs, key=lambda d: d.uri, similarity={})) == len(docs)


class TestSimilarityHelpers:
    """The matrices MMR consumes."""

    def test_jaccard_is_intersection_over_union(self) -> None:
        assert jaccard_similarity(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)

    def test_an_untagged_record_shares_nothing(self) -> None:
        """Not everything -- an empty union would otherwise divide by zero."""
        assert jaccard_similarity([], ["a"]) == 0.0
        assert jaccard_similarity([], []) == 0.0

    def test_duplicate_tags_do_not_change_the_score(self) -> None:
        assert jaccard_similarity(["a", "a"], ["a"]) == 1.0

    def test_tag_matrix_holds_both_orderings(self) -> None:
        """MMR looks a pair up in whichever order it meets it."""
        matrix = tag_similarity_matrix({"x": ["a"], "y": ["a"]})

        assert matrix[("x", "y")] == matrix[("y", "x")] == 1.0

    def test_tag_matrix_omits_zero_scoring_pairs(self) -> None:
        """A missing pair already reads as zero; storing it wastes memory."""
        assert tag_similarity_matrix({"x": ["a"], "y": ["b"]}) == {}

    def test_blend_weights_the_two_signals(self) -> None:
        blended = blend_similarity(
            sym({("x", "y"): 1.0}),
            sym({("x", "y"): 0.0}),
            embedding_weight=0.6,
            entity_weight=0.4,
        )

        assert blended[("x", "y")] == pytest.approx(0.6)

    def test_blend_covers_the_union_of_both_matrices(self) -> None:
        """A pair known to only one signal still has to appear."""
        blended = blend_similarity(sym({("x", "y"): 1.0}), sym({("p", "q"): 1.0}))

        assert set(blended) == {("x", "y"), ("y", "x"), ("p", "q"), ("q", "p")}

    def test_blend_stays_symmetric(self) -> None:
        """An asymmetric matrix makes MMR's output depend on candidate order."""
        blended = blend_similarity(sym({("x", "y"): 0.8}), sym({("x", "y"): 0.2}))

        assert blended[("x", "y")] == pytest.approx(blended[("y", "x")])

    def test_a_lone_signal_takes_the_full_weight(self) -> None:
        """Otherwise an exact duplicate could never score above 0.6.

        With the default split, scaling the only signal present would cap
        similarity below the level MMR needs to demote anything, and the
        diversity pass would silently stop working on any backend without
        pairwise similarity -- or on any corpus without tags.
        """
        embedding_only = blend_similarity(sym({("x", "y"): 1.0}), {})
        tags_only = blend_similarity({}, sym({("x", "y"): 1.0}))

        assert embedding_only[("x", "y")] == pytest.approx(1.0)
        assert tags_only[("x", "y")] == pytest.approx(1.0)

    def test_a_pair_missing_from_a_populated_matrix_still_scores_zero(self) -> None:
        """Two documents really can share no tags; that is data, not a gap."""
        blended = blend_similarity(
            sym({("x", "y"): 1.0}),
            sym({("p", "q"): 1.0}),
            embedding_weight=0.6,
            entity_weight=0.4,
        )

        assert blended[("x", "y")] == pytest.approx(0.6)

    def test_two_empty_matrices_blend_to_nothing(self) -> None:
        """No signal at all means MMR has nothing to work with."""
        assert blend_similarity({}, {}) == {}
