"""Maximal Marginal Relevance: trade a little relevance for less redundancy.

Ported from memex's ``memory/retrieval/engine.py`` (``_apply_mmr_diversity``,
``_build_hybrid_similarity_matrix``).

Retrieval that ranks on relevance alone returns the same document five ways.
MMR picks greedily, each time taking the candidate with the best
``lambda * relevance - (1 - lambda) * max_similarity_to_anything_already_picked``,
so a candidate that repeats an earlier pick has to be substantially more
relevant to earn its slot.

Similarity here is between *candidates*, never between a candidate and the
query. The caller supplies it as a matrix, which is what lets the expensive
half run wherever the vectors already live -- for the PostgreSQL backend, one
``pgvector`` query -- instead of shipping embeddings into Python.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import TypeVar

__all__ = [
    "DEFAULT_EMBEDDING_WEIGHT",
    "DEFAULT_ENTITY_WEIGHT",
    "DEFAULT_LAMBDA",
    "blend_similarity",
    "jaccard_similarity",
    "mmr_select",
    "tag_similarity_matrix",
]

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)

# 0.7 keeps relevance firmly in charge while still breaking up near-duplicates.
# 1.0 disables diversity entirely; 0.0 ranks on novelty alone and ignores the
# query.
DEFAULT_LAMBDA = 0.7

# memex's split between "these embed alike" and "these are about the same
# things". Two documents can be worded differently and still cover identical
# ground, which the tag overlap catches and the cosine does not.
DEFAULT_EMBEDDING_WEIGHT = 0.6
DEFAULT_ENTITY_WEIGHT = 0.4

# Scores within this distance count as tied, so a near-tie is settled by the
# caller's tiebreaker rather than by floating-point noise.
_TIE_EPSILON = 0.01


def mmr_select(
    items: Sequence[T],
    *,
    key: Callable[[T], K],
    similarity: Mapping[tuple[K, K], float],
    lambda_: float = DEFAULT_LAMBDA,
    limit: int | None = None,
    tiebreak: Callable[[T], float] | None = None,
) -> list[T]:
    """Re-order ``items`` to balance relevance against novelty.

    ``items`` must already be in relevance order, best first; relevance is
    taken from that position rather than from any score, so this works
    unchanged downstream of RRF, a cross-encoder, or a raw distance -- none of
    which produce comparable numbers.

    The first item is always kept: it is the most relevant thing available and
    has nothing to be redundant with.

    Parameters
    ----------
    items :
        Candidates in relevance order.
    key :
        Extracts the identity used to look up ``similarity``.
    similarity :
        Pairwise similarity in ``0..1``. A missing pair counts as ``0.0``, so
        a candidate whose similarity is unknown is treated as novel rather
        than silently suppressed. Supply both orderings of each pair, or
        canonicalise before calling.
    lambda_ :
        Weight on relevance. Must be in ``0..1``.
    limit :
        How many to select. ``None`` re-orders everything.
    tiebreak :
        Called on candidates whose MMR scores are within a hair of each other;
        the larger value wins. Use it to prefer newer records. Without it the
        earlier candidate keeps its place.

    Returns
    -------
    list[T]
        The selected items, most relevant first.

    Raises
    ------
    ValueError
        If ``lambda_`` is outside ``0..1``, where the arithmetic stops
        expressing a trade-off between the two terms.
    """
    if not 0.0 <= lambda_ <= 1.0:
        raise ValueError(f"MMR lambda must be between 0 and 1; got {lambda_}")
    if len(items) <= 1:
        return list(items)

    count = len(items)
    # Relevance from position, not score: an incoming score may be a distance,
    # an RRF sum, or a cross-encoder logit, and only their order is comparable.
    relevance = {key(item): (count - index) / count for index, item in enumerate(items)}

    remaining = list(items)
    selected: list[T] = [remaining.pop(0)]
    target = count if limit is None else min(limit, count)

    while remaining and len(selected) < target:
        best_index = 0
        best_score = -float("inf")

        for index, candidate in enumerate(remaining):
            candidate_key = key(candidate)
            redundancy = 0.0
            for chosen in selected:
                pair = (candidate_key, key(chosen))
                redundancy = max(redundancy, similarity.get(pair, 0.0))
            score = lambda_ * relevance[candidate_key] - (1.0 - lambda_) * redundancy

            if score > best_score + _TIE_EPSILON:
                best_score, best_index = score, index
            elif tiebreak is not None and abs(score - best_score) <= _TIE_EPSILON:
                if tiebreak(candidate) > tiebreak(remaining[best_index]):
                    best_score, best_index = score, index

        selected.append(remaining.pop(best_index))

    return selected


def jaccard_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """Return the overlap of two tag sets as intersection over union.

    Parameters
    ----------
    left, right :
        Tag lists. Order and duplicates are ignored.

    Returns
    -------
    float
        ``0.0`` when either side is empty, since an untagged record shares
        nothing rather than everything.
    """
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


def tag_similarity_matrix(
    tags_by_key: Mapping[K, Sequence[str]],
) -> dict[tuple[K, K], float]:
    """Build a symmetric Jaccard matrix over tag sets.

    memex compares extracted entities here. OpenViking already carries
    ``search_tags`` on every retrieval result, so the same signal costs no
    extra query.

    Parameters
    ----------
    tags_by_key :
        Tags per item key.

    Returns
    -------
    dict[tuple[K, K], float]
        Similarity for both orderings of each pair. Pairs scoring zero are
        omitted, since a missing pair already reads as zero.
    """
    keys = list(tags_by_key)
    matrix: dict[tuple[K, K], float] = {}
    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            score = jaccard_similarity(tags_by_key[left], tags_by_key[right])
            if score:
                matrix[(left, right)] = score
                matrix[(right, left)] = score
    return matrix


def blend_similarity(
    embedding: Mapping[tuple[K, K], float],
    entity: Mapping[tuple[K, K], float],
    *,
    embedding_weight: float = DEFAULT_EMBEDDING_WEIGHT,
    entity_weight: float = DEFAULT_ENTITY_WEIGHT,
) -> dict[tuple[K, K], float]:
    """Combine an embedding matrix and a tag-overlap matrix into one.

    When one signal is missing entirely -- a backend with no pairwise
    similarity, or candidates that carry no tags -- the other takes the full
    weight. Otherwise the surviving signal would be permanently scaled down:
    with the default split, embedding-only similarity would top out at 0.6, so
    even an exact duplicate could never look more than partly redundant and the
    diversity pass would quietly stop working.

    That decision is made per *matrix*, not per pair. A pair missing from one
    populated matrix genuinely scores zero there -- two documents really can
    share no tags -- and rescaling that away would invent overlap.

    Both inputs must hold each pair in both orderings, or the blend inherits
    whichever gap they have: a pair present in only one direction would take
    the other component's weight in that direction and not the other, making
    the result asymmetric and MMR's output depend on candidate order.

    Parameters
    ----------
    embedding :
        Pairwise cosine similarity.
    entity :
        Pairwise tag overlap.
    embedding_weight, entity_weight :
        Relative weights, applied only when both signals are present. They need
        not sum to 1, but the result is only in ``0..1`` when they do.

    Returns
    -------
    dict[tuple[K, K], float]
        The weighted sum over the union of both key sets, treating a pair
        absent from a populated matrix as zero there.
    """
    if not embedding:
        return dict(entity)
    if not entity:
        return dict(embedding)

    blended: dict[tuple[K, K], float] = {}
    for pair in set(embedding) | set(entity):
        blended[pair] = embedding_weight * embedding.get(
            pair, 0.0
        ) + entity_weight * entity.get(pair, 0.0)
    return blended
