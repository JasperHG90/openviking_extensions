"""Tests for the hybrid retriever's own logic.

The base retriever is stubbed out rather than run: exercising it for real needs
an embedder, a vector store and a server, which is what the integration suite
is for. What matters here is everything this package adds around it -- the
fusion, the diversity pass, and above all the ways they decline to run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest
from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
from openviking.server.identity import RequestContext, Role, UserIdentifier
from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend
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
        self.keyword_queries: list[str] = []
        self.scope_calls: list[dict[str, Any]] = []
        self.acl_on = False
        if similarity is not None:
            self._shared_adapter = _FakeAdapter(similarity)

    # Shaped like OpenViking 0.5's, so a call that omits `acl_enabled` fails
    # here as it does there. A permissive `**kwargs` fake let ov-ext 0.7 ship
    # a keyword leg that raised TypeError on every search.
    def _build_scope_filter(
        self,
        ctx: Any,
        context_type: Any,
        target_directories: Any,
        extra_filter: Any,
        level: Any = None,
        *,
        acl_enabled: bool,
    ) -> object:
        self.scope_calls.append({"ctx": ctx, "acl_enabled": acl_enabled})
        return {"scope": "built"}

    async def _acl_enabled(self, ctx: Any) -> bool:
        return self.acl_on

    async def search_by_keywords(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.keyword_calls += 1
        self.keyword_queries.append(str(kwargs.get("query", "")))
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
    """Build a matched context with the fields the retriever reads."""
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


async def test_a_document_sized_query_is_clipped_before_the_keyword_leg() -> None:
    """An agent pasting a whole document must not become a 2000-word disjunction.

    That matches nearly every row, so the leg stops narrowing anything, and
    the ranking then follows document length rather than relevance.
    """
    store = FakeStore(keyword_uris=["b"])
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)
    # Ten characters per word, so the 1024-character cut lands mid-word and
    # the boundary logic is actually exercised rather than skipped by luck.
    words = [f"w{n:04d}zzzz" for n in range(200)]
    document = " ".join(words)

    await retriever.retrieve(FakeQuery(query=document), ctx=None, limit=5)

    assert len(document) > 1024, "the query has to exceed the default ceiling"
    sent = store.keyword_queries[0]
    assert len(sent) <= 1024
    assert document.startswith(sent), "the clip keeps the head of the query"
    assert sent.split()[-1] in words, "a fragment matches rows nothing asked for"


async def test_a_normal_query_reaches_the_keyword_leg_untouched() -> None:
    """The clip is a safety valve; every real query has to pass through whole."""
    store = FakeStore(keyword_uris=["b"])
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)

    await retriever.retrieve(FakeQuery(query="vault policy rotation"), ctx=None, limit=5)

    assert store.keyword_queries == ["vault policy rotation"]


async def test_the_clip_can_be_turned_off() -> None:
    """Zero means send it all, the way the rerank ceilings read zero."""
    store = FakeStore(keyword_uris=["b"])
    retriever = make_retriever(
        store,
        [ctx("a"), ctx("b")],
        mmr_enabled=False,
        keyword_max_chars=0,
    )
    document = " ".join(f"word{n}" for n in range(500))

    await retriever.retrieve(FakeQuery(query=document), ctx=None, limit=5)

    assert store.keyword_queries == [document]


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


@pytest.mark.parametrize("acl_on", [False, True])
async def test_the_keyword_scope_carries_the_accounts_acl_setting(acl_on: bool) -> None:
    """The keyword leg asks the backend whether ACL is on and passes the answer."""
    store = FakeStore(keyword_uris=["b"])
    store.acl_on = acl_on
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)
    await retriever.retrieve(FakeQuery(), ctx="request", limit=2)
    assert store.scope_calls == [{"ctx": "request", "acl_enabled": acl_on}]
    assert store.keyword_calls == 1


class _AclManager:
    """Reports ACL as on or off for every account."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    async def is_enabled(self, account_id: str) -> bool:
        return self.enabled


class RealScopeStore(FakeStore):
    """A fake store running OpenViking's own scope builder, so its signature binds."""

    _acl_enabled = VikingVectorIndexBackend._acl_enabled
    _build_scope_filter = VikingVectorIndexBackend._build_scope_filter
    _tenant_filter = VikingVectorIndexBackend._tenant_filter
    _targets_within_visible_roots = staticmethod(
        VikingVectorIndexBackend._targets_within_visible_roots
    )
    _merge_filters = staticmethod(VikingVectorIndexBackend._merge_filters)

    def __init__(self, *, acl_enabled: bool, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.acl_manager = _AclManager(acl_enabled)
        self.filters: list[Any] = []

    async def search_by_keywords(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.filters.append(kwargs.get("filter"))
        return await super().search_by_keywords(**kwargs)


@pytest.mark.parametrize("acl_enabled", [False, True])
async def test_the_keyword_leg_runs_against_openvikings_scope_builder(
    caplog: pytest.LogCaptureFixture, acl_enabled: bool
) -> None:
    """With the real ``_build_scope_filter``, the leg runs and nothing is logged.

    ov-ext 0.7 called it without ``acl_enabled``; the TypeError was caught and
    every search went vector-only with a WARNING. With ACL on, the scope must
    carry the ACL filter, so a restricted resource cannot come back.
    """
    store = RealScopeStore(
        acl_enabled=acl_enabled, keyword_uris=["viking://resources/b.md"]
    )
    retriever = make_retriever(
        store,
        [ctx("viking://resources/a.md"), ctx("viking://resources/b.md")],
        mmr_enabled=False,
    )
    request = RequestContext(user=UserIdentifier("acme", "bob"), role=Role.USER)
    with caplog.at_level(logging.WARNING, logger="ov_ext.retrieval.retriever"):
        await retriever.retrieve(FakeQuery(), ctx=request, limit=2)
    assert store.keyword_calls == 1
    assert caplog.records == []
    scope = repr(store.filters[0])
    assert ("acl_mode" in scope) is acl_enabled


async def test_a_scope_that_cannot_be_built_is_an_error_not_a_quiet_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failing to scope the leg is logged at ERROR, and no unscoped search runs."""

    class BrokenScope(FakeStore):
        def _build_scope_filter(self, *args: Any, **kwargs: Any) -> object:
            raise TypeError("scope builder changed")

    store = BrokenScope(keyword_uris=["b"])
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)
    with caplog.at_level(logging.WARNING, logger="ov_ext.retrieval.retriever"):
        result = await retriever.retrieve(FakeQuery(), ctx=None, limit=2)
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert store.keyword_calls == 0
    assert [c.uri for c in result.matched_contexts] == ["a", "b"]


class AccountStore(FakeStore):
    """A store resolving each account to a backend of its own, as 0.5 does."""

    def __init__(self, account_similarity: dict[tuple[str, str], float], **kwargs: Any):
        super().__init__(similarity={}, **kwargs)
        self.account = _FakeAdapter(account_similarity)
        self.resolved: list[str] = []

    async def get_account_backend(self, account_id: str) -> Any:
        self.resolved.append(account_id)
        return SimpleNamespace(_adapter=self.account)


async def test_similarity_comes_from_the_requesting_accounts_own_backend() -> None:
    """A dedicated account's rows are not in the shared collection."""
    store = AccountStore({("a", "b"): 1.0, ("b", "a"): 1.0})
    retriever = make_retriever(
        store, [ctx("a"), ctx("b"), ctx("c")], keyword_enabled=False, mmr_lambda=0.5
    )
    request = SimpleNamespace(account_id="acme")
    result = await retriever.retrieve(FakeQuery(), ctx=request, limit=3)
    assert store.resolved == ["acme"]
    assert store.account.calls, "the account's adapter must be asked"
    assert store._shared_adapter.calls == []
    assert [c.uri for c in result.matched_contexts][1] == "c"


async def test_an_account_that_cannot_be_resolved_skips_diversity() -> None:
    """Never the shared adapter in its place: it would find no rows and say so."""

    class Unresolvable(AccountStore):
        async def get_account_backend(self, account_id: str) -> Any:
            raise RuntimeError("resolver not ready")

    store = Unresolvable({})
    retriever = make_retriever(
        store, [ctx("a"), ctx("b")], keyword_enabled=False, mmr_lambda=0.5
    )
    result = await retriever.retrieve(
        FakeQuery(), ctx=SimpleNamespace(account_id="acme"), limit=2
    )
    assert store._shared_adapter.calls == []
    assert [c.uri for c in result.matched_contexts] == ["a", "b"]


class _AccountAwareAdapter(_FakeAdapter):
    """Supplies similarity the way ov-postgres does, scoped to one account."""

    def pairwise_similarity(
        self,
        values: list[str],
        *,
        field: str | None = None,
        account_id: str | None = None,
    ) -> dict[tuple[str, str], float]:
        self.calls.append(
            {"values": list(values), "field": field, "account_id": account_id}
        )
        return self._similarity


async def test_similarity_is_scoped_to_the_requesting_account() -> None:
    """A shared table holds a URI once per account; the account is passed through."""
    store = FakeStore(similarity={})
    store._shared_adapter = _AccountAwareAdapter({("a", "b"): 1.0, ("b", "a"): 1.0})
    retriever = make_retriever(
        store, [ctx("a"), ctx("b"), ctx("c")], keyword_enabled=False, mmr_lambda=0.5
    )
    await retriever.retrieve(FakeQuery(), ctx=SimpleNamespace(account_id="acme"), limit=3)
    assert store._shared_adapter.calls[0]["account_id"] == "acme"
