"""Settings for the hybrid retrieval layer.

Read from the environment with an ``OV_RETRIEVAL_`` prefix, so a deployment can
turn the keyword leg or the diversity pass on and off without a code change and
without OpenViking's own config file learning about this package.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .diversity import (
    DEFAULT_EMBEDDING_WEIGHT,
    DEFAULT_ENTITY_WEIGHT,
    DEFAULT_LAMBDA,
)
from .fusion import RRF_K

__all__ = ["HybridSettings"]


class HybridSettings(BaseSettings):
    """How the hybrid retriever behaves.

    Attributes
    ----------
    keyword_enabled : bool
        Whether to run a lexical leg alongside the vector search and fuse the
        two. Off makes this package a no-op, which is the safe setting while a
        collection is still backfilling its bodies.
    keyword_weight : float
        Weight of the keyword ranking in the fusion, against 1.0 for vectors.
        Below 1.0 by default: the lexical leg is a corrective for terms the
        embedding misses, not an equal partner.
    rrf_k : int
        Smoothing constant for the fusion.
    pool_factor : int
        How many candidates to gather per requested result before fusing and
        diversifying. Re-ranking a list already cut to ``limit`` can only
        reorder what survived, so the pool has to be wider than the answer.
    mmr_enabled : bool
        Whether to apply the diversity pass.
    mmr_lambda : float
        Relevance against novelty. 1.0 disables diversity.
    mmr_embedding_weight, mmr_entity_weight : float
        Split between embedding cosine and tag overlap when measuring how much
        two candidates repeat each other.
    """

    model_config = SettingsConfigDict(env_prefix="OV_RETRIEVAL_", extra="forbid")

    keyword_enabled: bool = Field(
        default=True, description="Run a lexical leg and fuse it with the vector leg."
    )
    keyword_weight: float = Field(
        default=0.7,
        ge=0.0,
        description="Weight of the keyword ranking in RRF, against 1.0 for vectors.",
    )
    rrf_k: int = Field(default=RRF_K, ge=0, description="RRF smoothing constant.")
    pool_factor: int = Field(
        default=4,
        ge=1,
        description="Candidates gathered per requested result before re-ranking.",
    )
    mmr_enabled: bool = Field(
        default=True, description="Apply the MMR diversity pass before truncating."
    )
    mmr_lambda: float = Field(
        default=DEFAULT_LAMBDA,
        ge=0.0,
        le=1.0,
        description="Weight on relevance against novelty; 1.0 disables diversity.",
    )
    mmr_embedding_weight: float = Field(
        default=DEFAULT_EMBEDDING_WEIGHT,
        ge=0.0,
        description="Weight of embedding cosine in the similarity blend.",
    )
    mmr_entity_weight: float = Field(
        default=DEFAULT_ENTITY_WEIGHT,
        ge=0.0,
        description="Weight of search-tag overlap in the similarity blend.",
    )
