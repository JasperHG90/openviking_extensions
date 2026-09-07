"""Hybrid fusion and diversity re-ranking for OpenViking retrieval.

OpenViking retrieves by vector similarity and, when a reranker is configured,
re-scores the result with a cross-encoder. There is no lexical leg and no
diversity pass, so an exact term the embedding misses stays missed, and five
near-identical documents fill five slots.

This package adds both, in three layers:

``fusion``
    Reciprocal rank fusion. Pure, no OpenViking import.
``diversity``
    Maximal Marginal Relevance. Pure, no OpenViking import.
``retriever`` / ``install``
    Binds them to OpenViking by subclassing ``HierarchicalRetriever``.

Both algorithms are ported from memex, whose retrieval engine already runs
them in production.
"""

from __future__ import annotations

from .diversity import (
    DEFAULT_EMBEDDING_WEIGHT,
    DEFAULT_ENTITY_WEIGHT,
    DEFAULT_LAMBDA,
    blend_similarity,
    jaccard_similarity,
    mmr_select,
    tag_similarity_matrix,
)
from .fusion import RRF_K, rrf_fuse

__all__ = [
    "DEFAULT_EMBEDDING_WEIGHT",
    "DEFAULT_ENTITY_WEIGHT",
    "DEFAULT_LAMBDA",
    "RRF_K",
    "blend_similarity",
    "jaccard_similarity",
    "mmr_select",
    "rrf_fuse",
    "tag_similarity_matrix",
]
