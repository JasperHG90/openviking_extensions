"""Hybrid fusion and diversity re-ranking for OpenViking retrieval.

OpenViking retrieves by vector similarity and, when a reranker is configured,
re-scores the result with a cross-encoder. There is no lexical leg and no
diversity pass, so an exact term the embedding misses stays missed, and five
near-identical documents fill five slots.

This subsystem adds both, in three layers:

``fusion``
    Reciprocal rank fusion. Pure, no OpenViking import.
``diversity``
    Maximal Marginal Relevance. Pure, no OpenViking import.
``retriever`` / ``patch``
    Binds them to OpenViking by subclassing ``HierarchicalRetriever``.

Both algorithms are ported from memex, whose retrieval engine already runs
them in production.
"""

from __future__ import annotations

from .config import ENV_PREFIX, HybridSettings
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
from .patch import install, uninstall

__all__ = [
    "DEFAULT_EMBEDDING_WEIGHT",
    "DEFAULT_ENTITY_WEIGHT",
    "DEFAULT_LAMBDA",
    "ENV_PREFIX",
    "RRF_K",
    "HybridSettings",
    "blend_similarity",
    "install",
    "jaccard_similarity",
    "mmr_select",
    "rrf_fuse",
    "tag_similarity_matrix",
    "uninstall",
]
