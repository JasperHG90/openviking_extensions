"""A ``HierarchicalRetriever`` that also searches lexically and diversifies.

The vector leg is OpenViking's own ``retrieve``, called unchanged through
``super()``. Reimplementing it would mean reproducing the global recall,
reranking, thresholds and ACL scoping, and then keeping all of that in
step with upstream forever. Wrapping it costs one over-fetch and keeps every
one of those behaviours by construction.

What this adds around it:

1. a wider candidate pool, so the passes below have something to choose from;
2. a keyword search over the same scope, fused with the vector ranking by RRF;
3. an optional cap on the documents OpenViking's one rerank call sends, which
   matters because that call now scores the whole widened pool;
4. an MMR pass over the fused pool, so near-duplicates do not fill the answer.

Since OpenViking 0.4.23 the base ``retrieve`` runs one global vector search and
reranks the recalled candidates once, so the pool it returns has already been
scored as a whole. Nothing here reranks again: a second pass over the same
``abstract`` texts would mostly restore the order fusion just changed.

Every addition degrades to "do nothing" rather than to an error. A backend with
no keyword search, no pairwise similarity, or an OpenViking whose internals have
moved, all end at plain vector retrieval -- which is exactly what the user would
have had without this package installed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
from openviking.retrieve.rerank_scoring import RerankScores
from openviking.storage.viking_vector_index_backend import RETRIEVAL_OUTPUT_FIELDS
from openviking_cli.retrieve.types import MatchedContext, QueryResult, TypedQuery

from .config import HybridSettings
from .diversity import blend_similarity, mmr_select, tag_similarity_matrix
from .fusion import rrf_fuse
from ..observability import annotate, record_error, traced

__all__ = ["HybridRetriever"]

logger = logging.getLogger(__name__)

# Span and attribute prefix for this subsystem. Every name emitted from here is
# written out in full at its call site; this exists for `record_error`, which
# builds one rather than being handed one.
NAMESPACE = "ov_ext.retrieval"

# Ranker names, used as RRF keys and in the debug log.
_VECTOR = "vector"
_KEYWORD = "keyword"

# The whitespace-plus-word-tail a clip lands in the middle of, matched so the
# cut can fall before it rather than through it.
_TRAILING_FRAGMENT = re.compile(r"\s\S*\Z")


def _rank_below_the_model(
    scores: list[float],
    reranked: list[bool],
    fallback_scores: list[float],
    threshold: float = 0.0,
) -> None:
    """Move every score the model did not produce below every one that survives.

    Rewrites ``scores`` in place. The floor is the lowest model score above
    both zero and ``threshold`` -- a judged document under either is dropped
    by OpenViking anyway, and a reranker returning raw logits puts some below
    zero routinely. The unscored keep their order by vector score, spread
    evenly over the open interval between zero and that floor, so they stay
    above the default threshold of zero, which is the one applied to them.

    When no model score is positive there is no such interval. The unscored
    then go below every model score, one step apart, and that threshold drops
    them. Nothing changes when the model scored nothing, since then no scale
    was mixed.

    Parameters
    ----------
    scores :
        The merged scores, in input order.
    reranked :
        Per document, whether the model produced its score.
    fallback_scores :
        Vector scores, which order the unscored among themselves.
    threshold :
        The rerank threshold applied to model scores.
    """
    model = [score for score, scored in zip(scores, reranked, strict=True) if scored]
    if not model:
        return
    surviving = [score for score in model if score > max(threshold, 0.0)]
    unscored = sorted(
        (index for index, scored in enumerate(reranked) if not scored),
        key=lambda index: fallback_scores[index],
        reverse=True,
    )
    count = len(unscored)
    for rank, index in enumerate(unscored):
        if surviving:
            scores[index] = min(surviving) * (count - rank) / (count + 1)
        else:
            scores[index] = min(model) - (rank + 1)


def _clip_query(text: str, max_chars: int) -> str:
    """Cut ``text`` to ``max_chars``, ending on a whole word.

    Parameters
    ----------
    text :
        The query as the caller wrote it.
    max_chars :
        Ceiling on the result's length. Zero or less returns ``text``
        untouched, which is how the setting disables clipping.

    Returns
    -------
    str
        ``text`` itself when it already fits, otherwise a prefix of it no
        longer than ``max_chars``, ending where a word ends. The trailing
        partial word is dropped rather than kept: a fragment stems to a lexeme
        of its own and would match rows the query never mentioned. A single
        word longer than the ceiling has no such boundary and is cut mid-word.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    # The cut is already clean when the next character starts a new word, or
    # when the last one taken ended the previous word.
    if head[-1].isspace() or text[max_chars].isspace():
        return head.rstrip()
    # Cut at the whitespace before the fragment rather than rebuilding from
    # words, so the result stays a prefix of what the caller wrote. Stripped
    # like the branch above, or a cut landing in a run of spaces would end one
    # character past the word it promises to end on.
    boundary = _TRAILING_FRAGMENT.search(head)
    return head[: boundary.start()].rstrip() if boundary else head


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

    @traced("ov_ext.retrieval.retrieve")
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
        # `limit` is not merely a truncation knob upstream: it sizes the
        # global search, and with a reranker the recall is twice that, all of
        # it sent to the rerank service. Inflating it with both passes off
        # would pay for a wider pool nothing uses.
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
                "ov_ext.retrieval.limit": limit,
                "ov_ext.retrieval.pool": pool,
                "ov_ext.retrieval.keyword_enabled": settings.keyword_enabled,
                "ov_ext.retrieval.mmr_enabled": settings.mmr_enabled,
            }
        )

        result = await super().retrieve(query, ctx, limit=pool, **kwargs)
        contexts: list[MatchedContext] = list(result.matched_contexts)
        annotate({"ov_ext.retrieval.candidates": len(contexts)})
        if len(contexts) <= 1:
            # Nothing to fuse and nothing to diversify: one candidate is
            # already its own ranking.
            early = self._finish(result, contexts, limit)
            annotate(
                {
                    "ov_ext.retrieval.outcome": "too_few_candidates",
                    "ov_ext.retrieval.results": len(early.matched_contexts),
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
        annotate({"ov_ext.retrieval.results": len(finished.matched_contexts)})
        return finished

    @traced("ov_ext.retrieval.rerank")
    async def _rerank_scores(
        self,
        query: str,
        documents: list[str],
        fallback_scores: list[float],
    ) -> RerankScores:
        """Rerank as the base class does, sending at most ``rerank_max_documents``.

        The base retriever calls this once per search, over every candidate it
        recalled -- twice the pool, and the pool is ``pool_factor`` times the
        answer. The reranker charges by the document, so this is where a search
        spends. With the cap at zero it defers entirely; with a cap, only the
        strongest candidates by vector score are sent.

        The rest stay in the running, marked as not reranked, but **below
        every model score in the batch**, in their own vector order. OpenViking
        sorts the whole list by score, and a cosine compared against a rerank
        score is a comparison of two unrelated scales: left on their cosines,
        the weakest candidates -- the very ones the cap held back -- could
        outrank documents the model judged. A document the model was sent but
        declined to score is treated the same way.

        Parameters
        ----------
        query :
            Query text handed to the rerank model.
        documents :
            Candidate texts to score.
        fallback_scores :
            Vector scores to keep for whatever is not reranked.

        Returns
        -------
        RerankScores
            Scores in input order, and per document whether the model produced
            it.
        """
        annotate({"ov_ext.retrieval.documents": len(documents)})
        if not self._settings.rerank_max_documents:
            scored: RerankScores = await super()._rerank_scores(
                query, documents, fallback_scores
            )
            return scored
        chosen = self._choose_for_rerank(documents, fallback_scores)
        partial: RerankScores = await super()._rerank_scores(
            query,
            [documents[index] for index in chosen],
            [fallback_scores[index] for index in chosen],
        )
        scores = list(fallback_scores)
        reranked = [False] * len(documents)
        for index, score, from_model in zip(
            chosen, partial.scores, partial.reranked, strict=True
        ):
            scores[index] = score
            reranked[index] = from_model
        _rank_below_the_model(
            scores,
            reranked,
            fallback_scores,
            float(getattr(self, "rerank_threshold", 0.0) or 0.0),
        )
        annotate(
            {
                "ov_ext.retrieval.reranked_documents": sum(reranked),
                "ov_ext.retrieval.held_back_documents": len(documents) - len(chosen),
            }
        )
        return RerankScores(scores, reranked)

    def _choose_for_rerank(
        self, documents: list[str], fallback_scores: list[float]
    ) -> list[int]:
        """Return the indexes worth sending, in the batch's own order.

        Blanks are dropped before the cap applies, not after: the base class
        never sends a blank, so capping first could fill the whole allowance
        with documents the service would never see.

        The batch's own order is restored afterwards. The reranker scores each
        pair independently, so order cannot change a score, but it keeps the
        request, the span and the service's logs reading like an uncapped one.
        """
        chosen = [index for index, text in enumerate(documents) if text.strip()]
        cap = self._settings.rerank_max_documents
        if cap and len(chosen) > cap:
            chosen = sorted(
                chosen, key=lambda index: fallback_scores[index], reverse=True
            )[:cap]
            chosen.sort()
        return chosen

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
        compare and re-sort by ``score`` -- the context assembler's
        ``dedupe_keep_best`` keeps the higher one -- and would otherwise undo
        the fusion and the diversity pass entirely.

        The existing score *values* are handed out in the new order rather than
        replaced with RRF sums: that keeps the distribution the caller's
        thresholds were tuned against, while making the number agree with the
        position.

        The cost is that a score now means "rank within this answer" and not
        "similarity to this query", so comparing one across two separate calls
        is no longer meaningful. ``context_assembler/gather.py``'s
        ``dedupe_keep_best`` does, across the results of several searches. It
        was already comparing numbers a reranker had rewritten, so this narrows an
        existing looseness rather than introducing one -- but it is the reason
        to prefer redistributing the caller's own values over inventing new.
        """
        ranked = contexts[:limit]
        descending = sorted((context.score for context in ranked), reverse=True)
        for context, score in zip(ranked, descending):
            context.score = score
        result.matched_contexts = ranked
        return result

    @traced("ov_ext.retrieval.fuse_keywords")
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
        keyword-only hit would mean a result that skipped the vector recall and
        the rerank, and whose score means something different from
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
                "ov_ext.retrieval.vector_candidates": len(contexts),
                "ov_ext.retrieval.keyword_hits": len(keyword_uris),
            }
        )
        if not keyword_uris:
            annotate({"ov_ext.retrieval.outcome": "no_keyword_hits"})
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
        annotate(
            {"ov_ext.retrieval.fused": len(fused), "ov_ext.retrieval.outcome": "fused"}
        )
        return [by_uri[uri] for uri, _ in fused]

    @traced("ov_ext.retrieval.keyword_search")
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

        ``text`` is clipped to ``keyword_max_chars`` first. The vector leg can
        absorb a document-sized query; the lexical leg cannot use one.

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
            annotate({"ov_ext.retrieval.outcome": "backend_lacks_keyword_search"})
            return []

        # 1024 is borrowed from memex's `_MAX_SEED_QUERY_CHARS`, but only the
        # number is: that bound sits in its NER seed path, where a
        # document-sized query made entity extraction emit thousands of names
        # and blew the statement's parameter count. memex's own keyword leg
        # (`KeywordStrategy` in its `strategies.py`) runs unbounded. The shared
        # part is the trigger -- an agent passing a whole note as the query.
        clipped = _clip_query(text, self._settings.keyword_max_chars)
        if clipped != text:
            logger.debug(
                "hybrid: clipped a %d-character query to %d for the keyword leg",
                len(text),
                len(clipped),
            )

        acl_check = getattr(store, "_acl_enabled", None)
        if acl_check is None:
            # `install()` refuses to start without it; this keeps a store built
            # some other way from running the leg without its ACL scope.
            logger.debug("hybrid: backend cannot report ACL state; vector only")
            annotate({"ov_ext.retrieval.outcome": "backend_lacks_keyword_search"})
            return []

        try:
            scope = build_scope(
                ctx=ctx,
                context_type=context_type.value if context_type else None,
                target_directories=target_directories,
                extra_filter=scope_dsl,
                level=level,
                acl_enabled=await acl_check(ctx),
            )
        except Exception as exc:
            # Not "keyword search unavailable": the scope is what keeps the
            # leg inside the user's tenant and ACL, and failing to build it is
            # a fault worth an operator's attention, so it is logged at ERROR.
            # The search still answers from the vector leg alone.
            logger.error(
                "hybrid: could not build the keyword scope; vector only", exc_info=True
            )
            record_error(exc, "keyword_scope_failed", NAMESPACE)
            return []

        try:
            rows = await search(
                query=clipped,
                limit=pool,
                filter=scope,
                output_fields=RETRIEVAL_OUTPUT_FIELDS,
                ctx=ctx,
            )
        except NotImplementedError:
            # The built-in local backend cannot vectorise text and says so.
            # Expected, so it is annotated rather than recorded as an error.
            logger.debug("hybrid: backend does not implement keyword search")
            annotate({"ov_ext.retrieval.outcome": "not_implemented"})
            return []
        except Exception as exc:
            # A degraded ranking beats a failed search: the vector leg already
            # has an answer. Recorded on the span so the failure is visible --
            # nothing propagates for the tracer to catch by itself.
            logger.warning("hybrid: keyword leg failed; vector only", exc_info=True)
            record_error(exc, "keyword_search_failed", NAMESPACE)
            return []

        # Returned as stored, with no level suffix reconstructed. The caller
        # keys the fusion on the stored URI for the same reason.
        uris = [str(row["uri"]) for row in rows or [] if row.get("uri")]
        annotate(
            {"ov_ext.retrieval.keyword_hits": len(uris), "ov_ext.retrieval.outcome": "ok"}
        )
        return uris

    @traced("ov_ext.retrieval.diversify")
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
                "ov_ext.retrieval.candidates": len(contexts),
                "ov_ext.retrieval.embedding_pairs": len(embedding),
                "ov_ext.retrieval.tag_pairs": len(tags),
            }
        )
        if not embedding and not tags:
            # Neither signal is available, so every candidate looks equally
            # novel and MMR would only reproduce the input order.
            annotate({"ov_ext.retrieval.outcome": "no_similarity_signal"})
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
                "ov_ext.retrieval.selected": len(selected),
                "ov_ext.retrieval.outcome": "diversified",
            }
        )
        return selected

    @traced("ov_ext.retrieval.embedding_similarity")
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
        annotate({"ov_ext.retrieval.uris": len(uris)})
        adapter = getattr(self.vector_store, "_shared_adapter", None)
        similarity = getattr(adapter, "pairwise_similarity", None)
        if similarity is None:
            annotate({"ov_ext.retrieval.outcome": "backend_lacks_similarity"})
            return {}
        try:
            # By URI, not by row id: a retrieval result carries the URI, and
            # the backend picks one representative row per URI.
            pairs: dict[tuple[str, str], float] = await asyncio.to_thread(
                similarity, uris, field="uri"
            )
        except Exception as exc:
            logger.warning("hybrid: similarity failed; skipping diversity", exc_info=True)
            record_error(exc, "similarity_failed", NAMESPACE)
            return {}
        annotate({"ov_ext.retrieval.pairs": len(pairs), "ov_ext.retrieval.outcome": "ok"})
        return pairs
