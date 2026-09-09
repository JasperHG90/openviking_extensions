"""Settings for the hybrid retrieval layer.

Read from the environment with an ``OV_RETRIEVAL_`` prefix, so a deployment can
turn the keyword leg or the diversity pass on and off without a code change and
without OpenViking's own config file learning about this package.
"""

from __future__ import annotations

import os

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .diversity import (
    DEFAULT_EMBEDDING_WEIGHT,
    DEFAULT_ENTITY_WEIGHT,
    DEFAULT_LAMBDA,
)
from .fusion import RRF_K

__all__ = ["ENV_PREFIX", "HybridSettings"]

ENV_PREFIX = "OV_RETRIEVAL_"


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
    keyword_max_chars : int
        Characters of the query the keyword leg sees; 0 sends all of them.
        Bounds a document-sized query, which otherwise matches nearly every
        row and ranks by length.
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

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="forbid")

    @model_validator(mode="after")
    def _reject_misspelled_variables(self) -> HybridSettings:
        """Refuse an ``OV_RETRIEVAL_`` variable that matches no setting.

        ``extra="forbid"`` does not cover this. pydantic-settings looks up the
        fields it knows and never enumerates the environment, so
        ``OV_RETRIEVAL_MMR_LAMDA`` is not rejected -- it is never read at all,
        and the default silently stands. Someone who set it would see the
        diversity pass ignore them with nothing to explain why.

        Raises
        ------
        ValueError
            Naming the unknown variables and the settings that do exist.
        """
        known = {f"{ENV_PREFIX}{name}".upper() for name in type(self).model_fields}
        unknown = sorted(
            name
            for name in os.environ
            if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
        )
        if unknown:
            raise ValueError(
                f"Unknown setting(s): {', '.join(unknown)}. "
                f"Valid names are: {', '.join(sorted(known))}"
            )
        return self

    keyword_enabled: bool = Field(
        default=True, description="Run a lexical leg and fuse it with the vector leg."
    )
    keyword_weight: float = Field(
        default=0.7,
        ge=0.0,
        description="Weight of the keyword ranking in RRF, against 1.0 for vectors.",
    )
    keyword_max_chars: int = Field(
        default=1024,
        ge=0,
        description=(
            "Characters of the query the keyword leg sees; 0 sends all of "
            "them. An agent pasting a whole document as the query turns the "
            "lexical leg into a disjunction of every word in it, which matches "
            "nearly every row and then ranks by length, since the longest "
            "document matches the most words. Clipping keeps the leg doing its "
            "job -- catching exact terms the embedding missed. Real queries sit "
            "far below this, so it is a no-op for them."
        ),
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
    rerank_pooling: bool = Field(
        default=True,
        description=(
            "Route rerank calls through one pooled, trace-propagating "
            "connection instead of a fresh one per call. OpenViking reranks "
            "once per directory the descent visits, so a search opens hundreds "
            "of connections and the TCP and TLS handshake costs more than the "
            "inference."
        ),
    )
    rerank_max_calls: int = Field(
        default=0,
        ge=0,
        description=(
            "Ceiling on rerank calls during hierarchical descent; 0 removes "
            "the ceiling. Past it, candidates keep their vector scores -- the "
            "same degradation OpenViking already applies when reranking "
            "fails. Bounds the tail on a wide tree, where the descent has no "
            "cap of its own on how many directories it visits. The "
            "``rerank_final`` pass is not charged against it."
        ),
    )
    rerank_max_documents: int = Field(
        default=0,
        ge=0,
        description=(
            "Documents sent to the reranker in any one call; 0 sends all of "
            "them. The rest keep their vector scores and stay in the running. "
            "Cost is near-linear in documents, because the service batches "
            "them a few pairs at a time, so this is the knob that decides "
            "what a search spends. Prefer it to a low ``rerank_max_calls``: "
            "the same budget spread thinly over many directories reranks at "
            "every level of the descent, where a low call ceiling reranks the "
            "first few directories well and the rest not at all."
        ),
    )
    rerank_final: bool = Field(
        default=True,
        description=(
            "Rerank the pool once more, after fusion and before the diversity "
            "pass. The descent scores candidates as it meets them, one "
            "directory at a time, so nothing has ever scored the pool as a "
            "whole -- not the candidates the keyword leg promoted, and not "
            "the ones a spent call ceiling left on their vector scores. One "
            "call, and the last word on relevance."
        ),
    )
