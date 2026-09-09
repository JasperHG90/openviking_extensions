"""Tests for the hybrid retriever's own logic.

The base retriever is stubbed out rather than run: exercising it for real needs
an embedder, a vector store and a server, which is what the integration suite
is for. What matters here is everything this package adds around it -- the
fusion, the diversity pass, and above all the ways they decline to run.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
from openviking_cli.retrieve.types import MatchedContext, QueryResult

from ov_ext.retrieval.config import HybridSettings
from ov_ext.retrieval.retriever import HybridRetriever

pytestmark = pytest.mark.asyncio

# Base-class attributes a test replaced, restored after it.
monkeypatched: list[tuple[type, str]] = []


@pytest.fixture(autouse=True)
def restore_base_class() -> Iterator[None]:
    """Undo the stub on OpenViking's class, however the test ended.

    ``make_retriever`` patches ``HierarchicalRetriever.retrieve`` so the real
    ``HybridRetriever.retrieve`` can call ``super()``. That is process-global
    state on somebody else's class; leaving it set would change every later
    test in the session.
    """
    saved = {
        (cls, name): getattr(cls, name)
        for cls, name in [(HierarchicalRetriever, "retrieve")]
    }
    monkeypatched.clear()
    yield
    for (cls, name), original in saved.items():
        setattr(cls, name, original)
    monkeypatched.clear()


@dataclass
class FakeQuery:
    """Stands in for a TypedQuery."""

    query: str = "vault policy"
    context_type: Any = None
    target_directories: list[str] = field(default_factory=list)


class FakeStore:
    """A vector store exposing only what the hybrid retriever reaches for."""

    def __init__(
        self,
        *,
        keyword_uris: list[str] | None = None,
        similarity: dict[tuple[str, str], float] | None = None,
        keyword_error: Exception | None = None,
        levels: list[int] | None = None,
    ) -> None:
        self._keyword_uris = keyword_uris
        self._levels = levels
        self._similarity = similarity
        self._keyword_error = keyword_error
        self.keyword_calls = 0
        if similarity is not None:
            self._shared_adapter = _FakeAdapter(similarity)

    def _build_scope_filter(self, **kwargs: Any) -> object:
        return {"scope": "built"}

    async def search_by_keywords(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.keyword_calls += 1
        if self._keyword_error is not None:
            raise self._keyword_error
        uris = self._keyword_uris or []
        levels = self._levels or [2] * len(uris)
        return [
            {"uri": uri, "level": level} for uri, level in zip(uris, levels, strict=True)
        ]


class _FakeAdapter:
    """Supplies a similarity matrix the way ov-postgres does."""

    def __init__(self, similarity: dict[tuple[str, str], float]) -> None:
        self._similarity = similarity
        self.calls: list[dict[str, Any]] = []

    def pairwise_similarity(
        self, values: list[str], *, field: str | None = None
    ) -> dict[tuple[str, str], float]:
        self.calls.append({"values": list(values), "field": field})
        return self._similarity


def make_retriever(
    store: FakeStore, contexts: list[MatchedContext], **overrides: Any
) -> HybridRetriever:
    """Build a retriever that runs the *real* ``retrieve`` over a stubbed base.

    Only ``HierarchicalRetriever.retrieve`` is replaced; everything
    ``HybridRetriever`` adds runs for real, including its ``super()`` call.

    An earlier version of this helper reimplemented ``retrieve`` instead, which
    left the shipped method with no coverage at all -- a mutant raising on its
    first line passed the whole suite, and a real bug in it survived to review.
    """

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            # HierarchicalRetriever.__init__ wants a store, an embedder and
            # rerank config; none is reached once its `retrieve` is stubbed.
            self._settings = HybridSettings(**overrides)
            self.vector_store = store
            # Present, and truthy, because the final pass refuses to reorder
            # without one. Never called: the tests that reach the service
            # patch the base `_rerank_scores` over it.
            self._rerank_client = object()
            self.base_limit: int | None = None

    async def base_retrieve(
        self: Any, query: Any, request_ctx: Any, limit: int = 5, **kwargs: Any
    ) -> QueryResult:
        self.base_limit = limit
        return QueryResult(
            query=query,
            matched_contexts=[replace(c) for c in contexts],
            searched_directories=[],
        )

    # Patched on the base class, so the real `HybridRetriever.retrieve`'s
    # `super().retrieve(...)` lands here and the method under test runs.
    monkeypatched.append((HierarchicalRetriever, "retrieve"))
    HierarchicalRetriever.retrieve = base_retrieve
    return _Stubbed()


def ctx(
    uri: str,
    *,
    tags: list[str] | None = None,
    level: int = 2,
    score: float = 0.0,
    abstract: str = "",
) -> MatchedContext:
    """Build a matched context with the fields the retriever reads.

    ``abstract`` defaults to empty, which is what the final rerank pass reads.
    A pool of blank abstracts is refused before any call goes out, so tests
    that are not about that pass are unaffected by it.
    """
    return MatchedContext(
        uri=uri,
        context_type=None,
        level=level,
        score=score,
        abstract=abstract,
        search_tags=tags or [],
    )


async def test_keyword_ranking_reorders_the_vector_results() -> None:
    """The point of the keyword leg: promote what the embedding under-ranked."""
    store = FakeStore(keyword_uris=["c", "a"])
    retriever = make_retriever(store, [ctx("a"), ctx("b"), ctx("c")], mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=3)

    assert [m.uri for m in result.matched_contexts][0] == "a"
    assert [m.uri for m in result.matched_contexts].index("c") < 2


async def test_keyword_only_hits_are_not_promoted_into_the_answer() -> None:
    """A result that skipped hierarchical descent has an incomparable score."""
    store = FakeStore(keyword_uris=["never-seen", "b"])
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert {m.uri for m in result.matched_contexts} == {"a", "b"}


async def test_a_backend_without_keyword_search_still_retrieves() -> None:
    """Every non-PostgreSQL backend has to keep working."""

    class Bare:
        pass

    retriever = make_retriever(Bare(), [ctx("a"), ctx("b")], mmr_enabled=False)  # type: ignore[arg-type]

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert [m.uri for m in result.matched_contexts] == ["a", "b"]


async def test_a_backend_that_refuses_keyword_search_still_retrieves() -> None:
    """The built-in local backend raises NotImplementedError by design."""
    store = FakeStore(keyword_error=NotImplementedError("no vectoriser"))
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert [m.uri for m in result.matched_contexts] == ["a", "b"]


async def test_a_failing_keyword_leg_degrades_instead_of_raising() -> None:
    """The vector leg already has an answer; a partial ranking beats an error."""
    store = FakeStore(keyword_error=RuntimeError("connection reset"))
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert [m.uri for m in result.matched_contexts] == ["a", "b"]


async def test_an_empty_query_skips_the_keyword_leg() -> None:
    """A filter-only search has no text to match lexically."""
    store = FakeStore(keyword_uris=["b"])
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)

    await retriever.retrieve(FakeQuery(query="   "), ctx=None, limit=5)

    assert store.keyword_calls == 0


async def test_disabling_the_keyword_leg_skips_the_query_entirely() -> None:
    """Off must cost nothing, not merely change nothing."""
    store = FakeStore(keyword_uris=["b"])
    retriever = make_retriever(
        store, [ctx("a"), ctx("b")], keyword_enabled=False, mmr_enabled=False
    )

    await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert store.keyword_calls == 0


async def test_diversity_demotes_a_near_duplicate() -> None:
    """Two near-identical documents should not take two of five slots."""
    similarity = {("a", "a2"): 0.99, ("a2", "a"): 0.99}
    store = FakeStore(similarity=similarity)
    retriever = make_retriever(
        store, [ctx("a"), ctx("a2"), ctx("b")], keyword_enabled=False
    )

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=3)

    assert [m.uri for m in result.matched_contexts] == ["a", "b", "a2"]


async def test_similarity_is_looked_up_by_uri_not_row_id() -> None:
    """A retrieval result carries the URI; matching on the key finds nothing."""
    store = FakeStore(similarity={})
    retriever = make_retriever(store, [ctx("a"), ctx("b")], keyword_enabled=False)

    await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert store._shared_adapter.calls[0]["field"] == "uri"


async def test_tags_alone_can_drive_diversity() -> None:
    """Backends with no pairwise similarity still get some of the benefit."""

    class NoSimilarity(FakeStore):
        def __init__(self) -> None:
            super().__init__()

    store = NoSimilarity()
    retriever = make_retriever(
        store,
        [ctx("a", tags=["vault"]), ctx("a2", tags=["vault"]), ctx("b", tags=["dns"])],
        keyword_enabled=False,
    )

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=3)

    assert [m.uri for m in result.matched_contexts] == ["a", "b", "a2"]


async def test_the_pool_is_wider_than_the_requested_limit() -> None:
    """Re-ranking a list already cut to `limit` can only reorder the survivors."""
    store = FakeStore()
    retriever = make_retriever(store, [ctx("a"), ctx("b")], pool_factor=4)

    await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert retriever.base_limit == 20


async def test_the_answer_is_cut_back_to_the_requested_limit() -> None:
    """The caller asked for five, not the whole over-fetched pool."""
    store = FakeStore()
    retriever = make_retriever(store, [ctx(x) for x in "abcdefgh"])

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=3)

    assert len(result.matched_contexts) == 3


async def test_a_single_result_short_circuits_both_passes() -> None:
    """Nothing to fuse against and nothing to be redundant with."""
    store = FakeStore(keyword_uris=["a"])
    retriever = make_retriever(store, [ctx("a")])

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert [m.uri for m in result.matched_contexts] == ["a"]
    assert store.keyword_calls == 0


async def test_level_0_results_are_fused_despite_their_display_suffix() -> None:
    """The bug the first review caught, pinned.

    `_convert_to_matched_contexts` appends `/.abstract.md` to an L0 URI and
    `/.overview.md` to an L1 one, so a MatchedContext's URI is not what the
    `uri` column holds. Matching the two raw made the keyword leg a silent
    no-op for every L0 and L1 result -- no error, just no effect.
    """
    store = FakeStore(keyword_uris=["viking://docs/c", "viking://docs/a"], levels=[0, 0])
    contexts = [
        ctx("viking://docs/a/.abstract.md", level=0),
        ctx("viking://docs/b/.abstract.md", level=0),
        ctx("viking://docs/c/.abstract.md", level=0),
    ]
    retriever = make_retriever(store, contexts, mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=3)
    order = [m.uri for m in result.matched_contexts]

    assert order[0] == "viking://docs/a/.abstract.md"
    assert order.index("viking://docs/c/.abstract.md") < 2, "keyword leg had no effect"


async def test_similarity_is_queried_by_the_stored_uri_not_the_display_one() -> None:
    """The database has no row whose uri ends in `/.abstract.md`."""
    store = FakeStore(similarity={})
    contexts = [
        ctx("viking://docs/a/.overview.md", level=1),
        ctx("viking://docs/b", level=2),
    ]
    retriever = make_retriever(store, contexts, keyword_enabled=False)

    await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    asked = store._shared_adapter.calls[0]["values"]
    assert "viking://docs/a" in asked, "the level suffix must be stripped"
    assert "viking://docs/a/.overview.md" not in asked


async def test_scores_are_reordered_to_match_the_new_ranking() -> None:
    """Consumers re-sort by `.score`; a stale one undoes the fusion.

    `/skills/search` sorts `merged_hits` by score at
    `server/routers/skills.py:644`, so a ranking that lives only in list order
    is discarded there.
    """
    store = FakeStore(keyword_uris=["viking://docs/c"])
    contexts = [
        ctx("viking://docs/a", score=0.9),
        ctx("viking://docs/b", score=0.5),
        ctx("viking://docs/c", score=0.1),
    ]
    retriever = make_retriever(store, contexts, mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=3)
    scores = [m.score for m in result.matched_contexts]

    assert scores == sorted(scores, reverse=True), "score must agree with order"
    assert set(scores) == {0.9, 0.5, 0.1}, "the distribution must be preserved"


async def test_no_over_fetch_when_both_passes_are_off() -> None:
    """`limit` sizes the recursion upstream, so inflating it is not free."""
    store = FakeStore()
    retriever = make_retriever(
        store, [ctx("a"), ctx("b")], keyword_enabled=False, mmr_enabled=False
    )

    await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    assert retriever.base_limit == 5


async def test_the_real_retrieve_is_the_one_under_test() -> None:
    """Guards the helper: an earlier version reimplemented the method.

    If `make_retriever` ever stops routing through the shipped
    `HybridRetriever.retrieve`, every other test here becomes decorative.
    """
    assert (
        type(make_retriever(FakeStore(), [ctx("a")])).retrieve is HybridRetriever.retrieve
    )


async def test_the_legs_agree_when_they_meet_a_document_at_different_levels() -> None:
    """The seam the first suffix fix left open.

    Reconstructing the display suffix from the *keyword* row's level looks
    right and is not: the two legs can meet the same document at different
    levels -- the vector leg holding its L0 row, the keyword phrase sitting in
    its L2 body. The reconstructed URIs then differ and the hit is dropped,
    silently, which is the very failure the suffix fix was meant to end.

    Fusing on the stored URI makes the levels irrelevant.
    """
    store = FakeStore(keyword_uris=["viking://docs/target"], levels=[2])
    contexts = [
        ctx("viking://docs/decoy/.abstract.md", level=0),
        ctx("viking://docs/target/.abstract.md", level=0),
    ]
    retriever = make_retriever(store, contexts, mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

    assert result.matched_contexts[0].uri == "viking://docs/target/.abstract.md"


async def test_a_missing_upstream_suffix_table_degrades_rather_than_raising() -> None:
    """The module promises to degrade when an OpenViking internal moves."""
    store = FakeStore(keyword_uris=["viking://docs/b"])
    retriever = make_retriever(store, [ctx("viking://docs/a"), ctx("viking://docs/b")])
    saved = HierarchicalRetriever.LEVEL_URI_SUFFIX
    try:
        del HierarchicalRetriever.LEVEL_URI_SUFFIX

        result = await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

        assert len(result.matched_contexts) == 2
    finally:
        HierarchicalRetriever.LEVEL_URI_SUFFIX = saved


def rerank_by_abstract(monkeypatch: pytest.MonkeyPatch, best: str) -> list[list[str]]:
    """Stub the base rerank to prefer one abstract, recording each batch.

    Returns
    -------
    list
        The documents of every call that reached the base implementation.
    """
    seen: list[list[str]] = []

    async def base_rerank(
        self: Any, query: str, documents: list[str], fallback_scores: list[float]
    ) -> list[float]:
        seen.append(list(documents))
        return [1.0 if document == best else 0.0 for document in documents]

    monkeypatch.setattr(HierarchicalRetriever, "_rerank_scores", base_rerank)
    return seen


async def test_the_final_pass_reorders_the_fused_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass exists to overrule the order the descent and fusion produced.

    Both other passes are off, so nothing but the final rerank can move ``b``
    ahead of ``a``.
    """
    rerank_by_abstract(monkeypatch, best="good")
    retriever = make_retriever(
        FakeStore(),
        [ctx("a", abstract="dull"), ctx("b", abstract="good")],
        keyword_enabled=False,
        mmr_enabled=False,
    )

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

    assert [m.uri for m in result.matched_contexts] == ["b", "a"]


async def test_the_final_pass_runs_before_the_diversity_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters and is invisible in the output.

    ``mmr_select`` reads relevance from a candidate's position, so a rerank
    after it would quietly undo the diversification. Asserted by watching what
    ``_diversify`` is handed, since a wrong order here still produces a
    plausible-looking answer.
    """
    rerank_by_abstract(monkeypatch, best="good")
    handed: list[str] = []

    async def fake_diversify(
        self: Any, contexts: list[MatchedContext], *, limit: int
    ) -> list[MatchedContext]:
        handed.extend(context.uri for context in contexts)
        return contexts

    monkeypatch.setattr(HybridRetriever, "_diversify", fake_diversify)
    retriever = make_retriever(
        FakeStore(),
        [ctx("a", abstract="dull"), ctx("b", abstract="good")],
        keyword_enabled=False,
        mmr_enabled=True,
        mmr_lambda=0.5,
    )

    await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

    assert handed == ["b", "a"], (
        "diversity must see the reranked order, not the fused one"
    )


async def test_the_final_pass_is_not_charged_to_the_descent_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tight ceiling must not silently drop the pass that makes it affordable.

    The budget is set to zero -- fully spent -- and the pass must still call.
    """
    from ov_ext.retrieval.retriever import _rerank_budget

    seen = rerank_by_abstract(monkeypatch, best="good")

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings(rerank_max_calls=1)
            self._rerank_client = object()

    token = _rerank_budget.set(0)
    try:
        await _Stubbed()._rerank_pool([ctx("a", abstract="good")], text="q")
    finally:
        _rerank_budget.reset(token)

    assert seen == [["good"]], "the final pass must not consult the descent budget"


async def test_the_final_pass_keeps_the_fused_order_when_scoring_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short answer from the service must not reorder the pool by a wrong list."""

    async def short_rerank(
        self: Any, query: str, documents: list[str], fallback_scores: list[float]
    ) -> list[float]:
        return [1.0]

    monkeypatch.setattr(HierarchicalRetriever, "_rerank_scores", short_rerank)

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings()
            self._rerank_client = object()

    result = await _Stubbed()._rerank_pool(
        [ctx("a", abstract="x"), ctx("b", abstract="y")], text="q"
    )

    assert [m.uri for m in result] == ["a", "b"]


async def test_a_pool_of_blank_abstracts_costs_no_rerank_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary mid-backfill, and a round trip that could only learn nothing."""
    seen = rerank_by_abstract(monkeypatch, best="good")

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings()
            self._rerank_client = object()

    result = await _Stubbed()._rerank_pool([ctx("a"), ctx("b")], text="q")

    assert seen == [], "a pool with nothing to judge must not reach the service"
    assert [m.uri for m in result] == ["a", "b"]


def fused_pool() -> list[MatchedContext]:
    """A pool in fused order whose vector scores run the opposite way.

    This is the shape fusion produces whenever the keyword leg promotes a
    document the embedding ranked last, so sorting it by score is visibly
    wrong rather than merely unjustified.
    """
    return [
        ctx("buried", abstract="rotating the intermediate certificate", score=0.1),
        ctx("decoy", abstract="nothing of interest", score=0.9),
    ]


async def test_the_final_pass_keeps_the_fused_order_with_no_reranker_configured() -> None:
    """Regression: the end-to-end keyword test caught this and the unit suite did not.

    With no rerank client the base class hands ``fallback_scores`` straight
    back -- the pool's *vector* scores. Sorting by those undoes the fusion
    this pass runs after and puts the decoy back on top, which is exactly
    what the keyword leg exists to prevent.
    """

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings()
            self._rerank_client = None

    result = await _Stubbed()._rerank_pool(fused_pool(), text="intermediate certificate")

    assert [m.uri for m in result] == ["buried", "decoy"], (
        "fusion's order must survive a pass that scored nothing"
    )


async def test_the_final_pass_keeps_the_fused_order_when_the_service_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same bug, reached with a client configured.

    The base returns ``fallback_scores`` unchanged on a service error or an
    odd-length answer too, so a present-but-failing reranker must not reorder
    either. Checked separately because the no-client guard would mask it.
    """

    async def declines(
        self: Any, query: str, documents: list[str], fallback_scores: list[float]
    ) -> list[float]:
        return list(fallback_scores)

    monkeypatch.setattr(HierarchicalRetriever, "_rerank_scores", declines)

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings()
            self._rerank_client = object()

    result = await _Stubbed()._rerank_pool(fused_pool(), text="intermediate certificate")

    assert [m.uri for m in result] == ["buried", "decoy"], (
        "a declined rerank must not be read as a ranking"
    )
