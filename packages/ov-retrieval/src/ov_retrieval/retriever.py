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
from typing import Any

from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
from openviking.storage.viking_vector_index_backend import RETRIEVAL_OUTPUT_FIELDS
from openviking_cli.retrieve.types import MatchedContext, QueryResult, TypedQuery

from .config import HybridSettings
from .diversity import blend_similarity, mmr_select, tag_similarity_matrix
from .fusion import rrf_fuse

__all__ = ["HybridRetriever"]

logger = logging.getLogger(__name__)

# Ranker names, used as RRF keys and in the debug log.
_VECTOR = "vector"
_KEYWORD = "keyword"

# What a record is when its row does not say: L2, the detail level.
_DEFAULT_LEVEL = 2


def _level_of(row: dict[str, Any]) -> int:
    """Return a backend row's level, treating a missing or unusable one as L2.

    Written out rather than inlined as ``row.get("level", 2) or 2`` because
    level 0 is falsy, and L0 is precisely the level whose URI needs a suffix.
    """
    value = row.get("level", _DEFAULT_LEVEL)
    try:
        return int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LEVEL


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
        # back, and over-report `result_count` in the retrieval stats, for a
        # pass that never runs.
        pool = max(limit * settings.pool_factor, limit) if active else limit

        result = await super().retrieve(query, ctx, limit=pool, **kwargs)
        contexts: list[MatchedContext] = list(result.matched_contexts)
        if len(contexts) <= 1:
            return self._finish(result, contexts, limit)

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

        return self._finish(result, contexts, limit)

    @classmethod
    def _stored_uri(cls, context: MatchedContext) -> str:
        """Return the URI as the database holds it, without the level suffix.

        ``_convert_to_matched_contexts`` appends ``/.abstract.md`` for L0 and
        ``/.overview.md`` for L1, so a context's URI is not what the ``uri``
        column contains. The suffix table is the base class's own, which keeps
        this in step if upstream adds a level.
        """
        suffix = cls.LEVEL_URI_SUFFIX.get(context.level)
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
        """
        ranked = contexts[:limit]
        descending = sorted((context.score for context in ranked), reverse=True)
        for context, score in zip(ranked, descending):
            context.score = score
        result.matched_contexts = ranked
        return result

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
        if not keyword_uris:
            return contexts

        by_uri = {context.uri: context for context in contexts}
        fused = rrf_fuse(
            {
                _VECTOR: [context.uri for context in contexts],
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
        return [by_uri[uri] for uri, _ in fused]

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
            logger.debug("hybrid: backend does not implement keyword search")
            return []
        except Exception:
            # A degraded ranking beats a failed search: the vector leg already
            # has an answer.
            logger.warning("hybrid: keyword leg failed; vector only", exc_info=True)
            return []

        # The backend returns the *stored* URI; a MatchedContext carries the
        # *display* one, which for L0 and L1 has `/.abstract.md` or
        # `/.overview.md` appended by `_convert_to_matched_contexts`. Matching
        # the two raw would silently never line up for those levels, and the
        # keyword leg would do nothing for them while still logging hits.
        # `or 2` would be wrong here: level 0 is falsy and is exactly the case
        # that needs a suffix.
        return [
            self._append_level_suffix(str(row["uri"]), _level_of(row))
            for row in rows or []
            if row.get("uri")
        ]

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
        if not embedding and not tags:
            return contexts

        similarity = blend_similarity(
            embedding,
            tags,
            embedding_weight=settings.mmr_embedding_weight,
            entity_weight=settings.mmr_entity_weight,
        )
        return mmr_select(
            contexts,
            key=lambda context: context.uri,
            similarity=similarity,
            lambda_=settings.mmr_lambda,
            limit=limit,
        )

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
        adapter = getattr(self.vector_store, "_shared_adapter", None)
        similarity = getattr(adapter, "pairwise_similarity", None)
        if similarity is None:
            return {}
        try:
            # By URI, not by row id: a retrieval result carries the URI, and
            # the backend picks one representative row per URI.
            return await asyncio.to_thread(similarity, uris, field="uri")
        except Exception:
            logger.warning("hybrid: similarity failed; skipping diversity", exc_info=True)
            return {}
