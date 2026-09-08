"""A ``HierarchicalRetriever`` that also searches lexically and diversifies.

The vector leg is OpenViking's own ``retrieve``, called unchanged through
``super()``. Reimplementing it would mean reproducing hierarchical descent,
reranking, hotness blending and ACL scoping, and then keeping all of that in
step with upstream forever. Wrapping it costs one over-fetch and keeps every
one of those behaviours by construction.

What this adds around it:

1. a keyword search over the same scope, fused with the vector ranking by RRF;
2. an MMR pass over the fused pool, so near-duplicates do not fill the answer.

Every addition degrades to "do nothing" rather than to an error. A backend with
no keyword search, no pairwise similarity, or an OpenViking whose internals have
moved, all end at plain vector retrieval -- which is exactly what the user would
have had without this package installed.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import Any

from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
from openviking.storage.viking_vector_index_backend import RETRIEVAL_OUTPUT_FIELDS
from openviking_cli.retrieve.types import MatchedContext, QueryResult, TypedQuery

from .config import HybridSettings
from .diversity import blend_similarity, mmr_select, tag_similarity_matrix
from .fusion import rrf_fuse
from .observability import annotate, record_error, traced

__all__ = ["HybridRetriever"]

logger = logging.getLogger(__name__)

# Ranker names, used as RRF keys and in the debug log.
_VECTOR = "vector"
_KEYWORD = "keyword"

# Rerank calls still allowed in the retrieval currently running on this task.
# A ContextVar rather than an attribute because one retriever instance serves
# every concurrent request, so a counter on `self` would have them spending
# each other's budget. None means no ceiling.
_rerank_budget: ContextVar[int | None] = ContextVar(
    "ov_retrieval_rerank_budget", default=None
)


class HybridRetriever(HierarchicalRetriever):  # type: ignore[misc]  # base is untyped
    """Hierarchical retrieval plus a lexical leg and a diversity pass.

    Parameters
    ----------
    settings :
        Behaviour toggles. Read from the environment when omitted.
    **kwargs :
        Passed to :class:`HierarchicalRetriever` unchanged.
    """

    def __init__(self, *, settings: HybridSettings | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._settings = settings or HybridSettings()

    @traced("ov_retrieval.retrieve")
    async def retrieve(
        self,
        query: TypedQuery,
        ctx: Any,
        limit: int = 5,
        **kwargs: Any,
    ) -> QueryResult:
        """Retrieve, fuse a keyword ranking in, then diversify.

        Parameters
        ----------
        query :
            The typed query, whose ``query`` text feeds the keyword leg.
        ctx :
            Request context, carrying the tenant and permissions that scope
            both legs.
        limit :
            How many results the caller wants. The pool gathered internally is
            ``pool_factor`` times this.
        **kwargs :
            Forwarded to the base retriever. ``level`` and ``scope_dsl`` are
            also read here, so the keyword leg is scoped the same way.

        Returns
        -------
        QueryResult
            At most ``limit`` matched contexts.
        """
        settings = self._settings
        active = settings.keyword_enabled or (
            settings.mmr_enabled and settings.mmr_lambda < 1.0
        )
        # Over-fetch only when something will re-rank the extra candidates.
        # `limit` is not merely a truncation knob upstream -- it sizes the
        # global search, sizes child searches, and gates recursion -- so
        # inflating it with both features off would change which results come
        # back for a pass that never runs.
        #
        # When a pass *is* active the over-fetch is the point, and the cost is
        # that the base records `result_count` and `scores` for the whole pool
        # rather than the answer: retrieval stats read high by up to
        # `pool_factor`. The telemetry call happens inside `super().retrieve`,
        # so correcting it would mean reimplementing the method this class
        # exists to avoid reimplementing.
        pool = max(limit * settings.pool_factor, limit) if active else limit
        annotate(
            {
                "ov_retrieval.limit": limit,
                "ov_retrieval.pool": pool,
                "ov_retrieval.keyword_enabled": settings.keyword_enabled,
                "ov_retrieval.mmr_enabled": settings.mmr_enabled,
            }
        )

        # Set before the base retriever runs, since the descent it drives is
        # what spends the budget. The token is reset in `finally` so a task
        # reused for the next request does not inherit what is left.
        token = _rerank_budget.set(settings.rerank_max_calls or None)
        try:
            result = await super().retrieve(query, ctx, limit=pool, **kwargs)
        finally:
            _rerank_budget.reset(token)
        contexts: list[MatchedContext] = list(result.matched_contexts)
        annotate({"ov_retrieval.candidates": len(contexts)})
        if len(contexts) <= 1:
            # Nothing to fuse and nothing to diversify: one candidate is
            # already its own ranking.
            early = self._finish(result, contexts, limit)
            annotate(
                {
                    "ov_retrieval.outcome": "too_few_candidates",
                    "ov_retrieval.results": len(early.matched_contexts),
                }
            )
            return early

        text = (getattr(query, "query", "") or "").strip()
        if settings.keyword_enabled and text:
            contexts = await self._fuse_keywords(
                contexts,
                text=text,
                ctx=ctx,
                pool=pool,
                scope_dsl=kwargs.get("scope_dsl"),
                level=kwargs.get("level"),
                context_type=query.context_type,
                target_directories=getattr(query, "target_directories", None),
            )

        if settings.mmr_enabled and settings.mmr_lambda < 1.0:
            contexts = await self._diversify(contexts, limit=limit)

        finished = self._finish(result, contexts, limit)
        annotate({"ov_retrieval.results": len(finished.matched_contexts)})
        return finished

    @traced("ov_retrieval.rerank")
    async def _rerank_scores(
        self,
        query: str,
        documents: list[str],
        fallback_scores: list[float],
    ) -> list[float]:
        """Rerank as the base class does, under a per-retrieval call ceiling.

        The base retriever calls this once per directory the descent visits,
        serially, and caps neither the calls nor the directories. On a wide
        tree that is hundreds of round trips for one search. This adds the
        ceiling ``rerank_max_calls`` and otherwise defers entirely.

        Exhausting the budget returns ``fallback_scores`` -- the vector
        ordering -- which is the same degradation the base class already
        applies when the rerank client fails or answers oddly. So the worst
        case is a ranking OpenViking itself considers acceptable, rather than
        an error.

        Parameters
        ----------
        query :
            Query text handed to the rerank model.
        documents :
            Candidate texts to score.
        fallback_scores :
            Vector scores to keep when reranking is skipped.

        Returns
        -------
        list[float]
            Rerank scores, or ``fallback_scores`` when the budget is spent.
        """
        annotate({"ov_retrieval.documents": len(documents)})
        # Spend nothing on a batch the base class will refuse anyway: it
        # returns early when every document is blank, without calling the
        # service. A subtree of directories with no abstract yet -- ordinary
        # during a backfill -- would otherwise burn the whole ceiling before
        # one real rerank had happened.
        if not any(document.strip() for document in documents):
            annotate({"ov_retrieval.outcome": "nothing_to_rerank"})
            return fallback_scores

        remaining = _rerank_budget.get()
        if remaining is not None:
            if remaining <= 0:
                # Recorded rather than logged: a search that silently stopped
                # reranking half way looks identical to one that never had a
                # reranker, and the scores do not say which.
                annotate({"ov_retrieval.outcome": "rerank_budget_spent"})
                return fallback_scores
            _rerank_budget.set(remaining - 1)

        scores: list[float] = await super()._rerank_scores(
            query, documents, fallback_scores
        )
        annotate({"ov_retrieval.outcome": "reranked"})
        return scores

    @classmethod
    def _stored_uri(cls, context: MatchedContext) -> str:
        """Return the URI as the database holds it, without the level suffix.

        ``_convert_to_matched_contexts`` appends ``/.abstract.md`` for L0 and
        ``/.overview.md`` for L1, so a context's URI is not what the ``uri``
        column contains. The suffix table is the base class's own, which keeps
        this in step if upstream adds a level.

        Read through ``getattr`` rather than as an attribute: this module
        promises to degrade to plain vector retrieval when an OpenViking
        internal moves, and a bare access here would raise instead. Without the
        table every URI is returned as-is, which costs the L0/L1 candidates
        their fusion and diversity but keeps the search answering.
        """
        table: dict[int, str] = getattr(cls, "LEVEL_URI_SUFFIX", {})
        suffix = table.get(context.level)
        if suffix and context.uri.endswith(f"/{suffix}"):
            return str(context.uri[: -(len(suffix) + 1)])
        return str(context.uri)

    @staticmethod
    def _finish(
        result: QueryResult, contexts: list[MatchedContext], limit: int
    ) -> QueryResult:
        """Return ``result`` carrying at most ``limit`` of ``contexts``.

        Scores are redistributed so they agree with the new order. Consumers
        re-sort by ``score`` -- ``/skills/search`` does, at
        ``server/routers/skills.py:644`` -- and would otherwise undo the
        fusion and the diversity pass entirely.

        The existing score *values* are handed out in the new order rather than
        replaced with RRF sums: that keeps the distribution the caller's
        thresholds were tuned against, while making the number agree with the
        position.

        The cost is that a score now means "rank within this answer" and not
        "similarity to this query", so comparing one across two separate calls
        is no longer meaningful. Two places do:
        ``context_assembler/gather.py``'s ``dedupe_keep_best`` and
        ``server/routers/skills.py``'s merge of two ``find`` results. Both were
        already comparing numbers a reranker had rewritten, so this narrows an
        existing looseness rather than introducing one -- but it is the reason
        to prefer redistributing the caller's own values over inventing new.
        """
        ranked = contexts[:limit]
        descending = sorted((context.score for context in ranked), reverse=True)
        for context, score in zip(ranked, descending):
            context.score = score
        result.matched_contexts = ranked
        return result

    @traced("ov_retrieval.fuse_keywords")
    async def _fuse_keywords(
        self,
        contexts: list[MatchedContext],
        *,
        text: str,
        ctx: Any,
        pool: int,
        scope_dsl: Any,
        level: Any,
        context_type: Any,
        target_directories: Any,
    ) -> list[MatchedContext]:
        """Reorder ``contexts`` by fusing a keyword ranking into their order.

        Only candidates the vector leg already found are reordered. Promoting a
        keyword-only hit would mean a result that skipped hierarchical descent,
        reranking and hotness, and whose score means something different from
        every other result's.

        Returns
        -------
        list[MatchedContext]
            The same contexts, in fused order. Unchanged if the keyword leg is
            unavailable or found nothing.
        """
        keyword_uris = await self._keyword_uris(
            text=text,
            ctx=ctx,
            pool=pool,
            scope_dsl=scope_dsl,
            level=level,
            context_type=context_type,
            target_directories=target_directories,
        )
        annotate(
            {
                "ov_retrieval.vector_candidates": len(contexts),
                "ov_retrieval.keyword_hits": len(keyword_uris),
            }
        )
        if not keyword_uris:
            annotate({"ov_retrieval.outcome": "no_keyword_hits"})
            return contexts

        # Fuse on the *stored* URI, which both legs agree on. A context's own
        # URI carries a level suffix and the keyword row's does not, and the
        # two legs can meet the same document at different levels -- the vector
        # leg at L0, the keyword hit on that URI's L2 row. Reconstructing the
        # suffix from the keyword row's level looks right and is not: it makes
        # the two disagree in exactly that case, and the hit is dropped.
        # Upstream keeps one candidate per stored URI, so this key is unique.
        by_uri = {self._stored_uri(context): context for context in contexts}
        fused = rrf_fuse(
            {
                _VECTOR: [self._stored_uri(context) for context in contexts],
                # Restricted to what the vector leg found, so the fusion ranks
                # one candidate set rather than stitching two together.
                _KEYWORD: [uri for uri in keyword_uris if uri in by_uri],
            },
            weights={_KEYWORD: self._settings.keyword_weight},
            k=self._settings.rrf_k,
        )
        logger.debug(
            "hybrid: %d vector, %d keyword, %d fused",
            len(contexts),
            len(keyword_uris),
            len(fused),
        )
        annotate({"ov_retrieval.fused": len(fused), "ov_retrieval.outcome": "fused"})
        return [by_uri[uri] for uri, _ in fused]

    @traced("ov_retrieval.keyword_search")
    async def _keyword_uris(
        self,
        *,
        text: str,
        ctx: Any,
        pool: int,
        scope_dsl: Any,
        level: Any,
        context_type: Any,
        target_directories: Any,
    ) -> list[str]:
        """Return URIs ranked lexically, best first.

        Scoping reuses the backend's own ``_build_scope_filter`` rather than a
        hand-built equivalent: it is where tenant isolation, ACL and
        target-directory limits live, and a second implementation would be one
        upstream change away from quietly disagreeing with the vector leg about
        what a user may see. If it is missing -- an upstream rename -- the
        keyword leg is skipped rather than run unscoped.

        Returns
        -------
        list[str]
            Ranked URIs, or empty when keyword search is unavailable.
        """
        store = self.vector_store
        build_scope = getattr(store, "_build_scope_filter", None)
        search = getattr(store, "search_by_keywords", None)
        if build_scope is None or search is None:
            logger.debug("hybrid: backend has no scoped keyword search; vector only")
            annotate({"ov_retrieval.outcome": "backend_lacks_keyword_search"})
            return []

        try:
            scope = build_scope(
                ctx=ctx,
                context_type=context_type.value if context_type else None,
                target_directories=target_directories,
                extra_filter=scope_dsl,
                level=level,
            )
            rows = await search(
                query=text,
                limit=pool,
                filter=scope,
                output_fields=RETRIEVAL_OUTPUT_FIELDS,
                ctx=ctx,
            )
        except NotImplementedError:
            # The built-in local backend cannot vectorise text and says so.
            # Expected, so it is annotated rather than recorded as an error.
            logger.debug("hybrid: backend does not implement keyword search")
            annotate({"ov_retrieval.outcome": "not_implemented"})
            return []
        except Exception as exc:
            # A degraded ranking beats a failed search: the vector leg already
            # has an answer. Recorded on the span so the failure is visible --
            # nothing propagates for the tracer to catch by itself.
            logger.warning("hybrid: keyword leg failed; vector only", exc_info=True)
            record_error(exc, "keyword_search_failed")
            return []

        # Returned as stored, with no level suffix reconstructed. The caller
        # keys the fusion on the stored URI for the same reason.
        uris = [str(row["uri"]) for row in rows or [] if row.get("uri")]
        annotate({"ov_retrieval.keyword_hits": len(uris), "ov_retrieval.outcome": "ok"})
        return uris

    @traced("ov_retrieval.diversify")
    async def _diversify(
        self, contexts: list[MatchedContext], *, limit: int
    ) -> list[MatchedContext]:
        """Re-rank ``contexts`` so near-duplicates fall below novel results.

        Similarity blends embedding cosine, computed inside the database, with
        overlap of ``search_tags``, which every retrieval result already
        carries. Two documents can be worded differently and still cover the
        same ground; the tags catch that and the cosine does not.

        Returns
        -------
        list[MatchedContext]
            Diversified order, or the input unchanged when no similarity
            signal is available at all.
        """
        settings = self._settings
        # Two URI spaces meet here. The database knows the stored URI; a
        # MatchedContext carries the display one, suffixed for L0 and L1. Query
        # by the former, then translate the matrix back, or no L0/L1 candidate
        # would ever be found similar to anything.
        stored_by_display = {
            context.uri: self._stored_uri(context) for context in contexts
        }
        display_by_stored = {
            stored: display for display, stored in stored_by_display.items()
        }
        embedding = {
            (display_by_stored[left], display_by_stored[right]): score
            for (left, right), score in (
                await self._embedding_similarity(list(display_by_stored))
            ).items()
            if left in display_by_stored and right in display_by_stored
        }
        tags = tag_similarity_matrix(
            {context.uri: context.search_tags or [] for context in contexts}
        )
        annotate(
            {
                "ov_retrieval.candidates": len(contexts),
                "ov_retrieval.embedding_pairs": len(embedding),
                "ov_retrieval.tag_pairs": len(tags),
            }
        )
        if not embedding and not tags:
            # Neither signal is available, so every candidate looks equally
            # novel and MMR would only reproduce the input order.
            annotate({"ov_retrieval.outcome": "no_similarity_signal"})
            return contexts

        similarity = blend_similarity(
            embedding,
            tags,
            embedding_weight=settings.mmr_embedding_weight,
            entity_weight=settings.mmr_entity_weight,
        )
        selected = mmr_select(
            contexts,
            key=lambda context: context.uri,
            similarity=similarity,
            lambda_=settings.mmr_lambda,
            limit=limit,
        )
        annotate(
            {
                "ov_retrieval.selected": len(selected),
                "ov_retrieval.outcome": "diversified",
            }
        )
        return selected

    @traced("ov_retrieval.embedding_similarity")
    async def _embedding_similarity(
        self, uris: list[str]
    ) -> dict[tuple[str, str], float]:
        """Return pairwise cosine similarity between candidates, if the backend can.

        The vectors stay in the database: OpenViking excludes them from
        retrieval output deliberately, and fetching them back would be the
        largest payload in the system, per query, to compute a number the
        database can produce itself.

        Runs off the event loop, since the backend method is synchronous and
        issues a query.

        Returns
        -------
        dict[tuple[str, str], float]
            Similarity per URI pair in both orderings, or empty when the
            backend offers no such method.
        """
        annotate({"ov_retrieval.uris": len(uris)})
        adapter = getattr(self.vector_store, "_shared_adapter", None)
        similarity = getattr(adapter, "pairwise_similarity", None)
        if similarity is None:
            annotate({"ov_retrieval.outcome": "backend_lacks_similarity"})
            return {}
        try:
            # By URI, not by row id: a retrieval result carries the URI, and
            # the backend picks one representative row per URI.
            pairs: dict[tuple[str, str], float] = await asyncio.to_thread(
                similarity, uris, field="uri"
            )
        except Exception as exc:
            logger.warning("hybrid: similarity failed; skipping diversity", exc_info=True)
            record_error(exc, "similarity_failed")
            return {}
        annotate({"ov_retrieval.pairs": len(pairs), "ov_retrieval.outcome": "ok"})
        return pairs
