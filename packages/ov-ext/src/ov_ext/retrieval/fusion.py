"""Reciprocal rank fusion for combining several rankings of the same items.

Ported from memex's ``memory/retrieval/document_search.py``, which fuses its
retrieval strategies with ``sum(weight / (K_RRF + rank))``.

RRF reads *positions*, never scores. That is the whole reason to use it here:
a cosine distance and a ``ts_rank_cd`` value are not comparable quantities and
no fixed weighting makes them so, but "third best by vector" and "first best by
keyword" combine perfectly well. It also means any monotonic rescaling of a
ranker's scores is invisible to the fusion, so a ranker can be retuned without
recalibrating anything here.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import TypeVar

__all__ = ["RRF_K", "rrf_fuse"]

K = TypeVar("K", bound=Hashable)

# Smoothing constant from Cormack et al.'s original RRF paper, and the value
# memex uses. Large relative to the ranks that matter, so the gap between rank
# 1 and rank 2 stays modest and one ranker cannot dominate on its top hit
# alone. Lowering it sharpens the preference for first place.
RRF_K = 60


def rrf_fuse(
    rankings: Mapping[str, Sequence[K]],
    *,
    weights: Mapping[str, float] | None = None,
    k: int = RRF_K,
    limit: int | None = None,
) -> list[tuple[K, float]]:
    """Fuse several rankings of the same items into one.

    Each ranking contributes ``weight / (k + rank)`` per item, with ``rank``
    counted from 1. An item appearing in several rankings accumulates a
    contribution from each, so agreement between rankers is what lifts an item
    rather than a strong showing in any single one.

    An item absent from a ranking simply scores nothing there. That is the
    intended behavior and not a gap to fill with a default: a keyword search
    that cannot match a document should not be forced to rank it.

    Parameters
    ----------
    rankings :
        Ordered item keys per ranker name, best first. Duplicate keys within
        one ranking are ignored after their first, best position.
    weights :
        Per-ranker multiplier. A ranker absent from the mapping weighs 1.0.
        A weight of zero excludes a ranker without changing the call site.
    k :
        Smoothing constant; see :data:`RRF_K`.
    limit :
        Truncate to this many results. ``None`` returns everything seen.

    Returns
    -------
    list[tuple[K, float]]
        Item key and fused score, highest first. Ties break on the item's best
        position across all rankings and then on its key, so the order depends
        only on the rankings themselves and not on which ranker the caller
        listed first.

    Raises
    ------
    ValueError
        If ``k`` is negative, which would let rank 1 divide by zero or invert
        the ranking's sign.
    """
    if k < 0:
        raise ValueError(f"RRF k must not be negative; got {k}")

    scores: dict[K, float] = {}
    best_rank: dict[K, int] = {}

    for name, ordered in rankings.items():
        weight = 1.0 if weights is None else weights.get(name, 1.0)
        if weight == 0.0:
            continue
        seen: set[K] = set()
        for position, key in enumerate(ordered, start=1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + weight / (k + position)
            if key not in best_rank or position < best_rank[key]:
                best_rank[key] = position

    # The final `str` is what makes the order independent of the caller's dict
    # ordering. Two items each ranked first by a different ranker tie on score
    # *and* on best position, and without a last resort the winner would be
    # whichever ranker the caller happened to list first.
    ranked = sorted(scores, key=lambda key: (-scores[key], best_rank[key], str(key)))
    if limit is not None:
        ranked = ranked[:limit]
    return [(key, scores[key]) for key in ranked]
