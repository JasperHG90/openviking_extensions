"""Coverage for connection pooling and the per-retrieval rerank ceiling.

Both exist for the same reason: OpenViking reranks once per directory the
hierarchical descent visits, serially, with no cap on either count. A single
search was observed making hundreds of calls, each paying a fresh TCP and TLS
handshake -- which cost more than the inference it wrapped.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
from openviking_cli.retrieve.types import QueryResult
from .test_retriever import FakeQuery

from ov_ext.retrieval.config import HybridSettings
from ov_ext.observability import _tracer
from ov_ext.retrieval.rerank import (
    _PooledRequests,
    install_pooled_rerank,
    uninstall_pooled_rerank,
)
from ov_ext.retrieval.retriever import HybridRetriever, _rerank_budget

_OPENAI_RERANK = "openviking.models.rerank.openai_rerank"


class FakeSession:
    """Records calls instead of making them.

    Counts `post` and `request` separately, since routing one to the other is
    a mistake that would raise on every real call.
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.requests = 0

    def post(self, *args: Any, **kwargs: Any) -> str:
        """Record one post and return a stand-in response."""
        self.posts.append(kwargs)
        return "response"

    def request(self, *args: Any, **kwargs: Any) -> str:
        """Record one request and return a stand-in response."""
        self.requests += 1
        return "response"


@pytest.fixture(autouse=True)
def restore_rerank_patch() -> Iterator[None]:
    """Guarantee an unpatched module before and after every test here.

    Both ends matter. Undoing afterwards keeps this file from changing later
    tests -- it is process-global state on somebody else's package. Undoing
    *first* matters because ``install()`` now installs pooling too, so any
    earlier test that patched the retriever leaves this one starting from an
    already-pooled module, and an assertion about installing would read the
    previous test's work as its own.
    """
    uninstall_pooled_rerank()
    yield
    uninstall_pooled_rerank()


def test_the_shim_sends_through_the_pooled_session() -> None:
    """Every call goes to one session, so the connection is reused."""
    import requests

    session = FakeSession()
    shim = _PooledRequests(session, requests)  # type: ignore[arg-type]

    shim.post(url="https://rerank.example/v1", headers={}, json={"documents": ["a"]})
    shim.post(url="https://rerank.example/v1", headers={}, json={"documents": ["b"]})

    assert len(session.posts) == 2


def test_the_shim_carries_the_trace_context(spans: InMemorySpanExporter) -> None:
    """A traceparent header is what joins the rerank service's spans to ours.

    Without it the service traces its own work into a separate trace, which is
    how a slow rerank stays invisible from the retrieval that caused it.
    """
    import requests

    session = FakeSession()
    shim = _PooledRequests(session, requests)  # type: ignore[arg-type]
    headers: dict[str, str] = {"Authorization": "Bearer x"}

    with _tracer().start_as_current_span("ov_ext.retrieval.parent"):
        shim.post(url="https://rerank.example/v1", headers=headers, json={})

    assert "traceparent" in headers
    # The original headers survive; injection adds rather than replaces.
    assert headers["Authorization"] == "Bearer x"


def test_the_shim_records_how_many_documents_it_sent(
    spans: InMemorySpanExporter,
) -> None:
    """Batch size is the number that says whether call count is the problem."""
    import requests

    shim = _PooledRequests(FakeSession(), requests)  # type: ignore[arg-type]

    shim.post(url="https://rerank.example/v1", headers={}, json={"documents": ["a", "b"]})

    call = next(
        s for s in spans.get_finished_spans() if s.name == "ov_ext.retrieval.rerank_call"
    )
    assert call.attributes is not None
    assert call.attributes["ov_ext.retrieval.documents"] == 2


def test_the_shim_sends_a_request_call_through_the_session_too() -> None:
    """The VikingDB client uses `request`, not `post`.

    Implementing only `post` left its calls falling through to the real module
    -- a connection each -- while the install log said they were pooled.
    """
    import requests

    session = FakeSession()
    shim = _PooledRequests(session, requests)  # type: ignore[arg-type]

    shim.request(method="POST", url="https://rerank.example/api", headers={})

    assert session.requests == 1, "the call must reach the pooled session"


def test_a_request_call_is_not_routed_through_post() -> None:
    """`Session.post` takes no `method`, so routing there would raise on every call."""
    import requests

    session = FakeSession()
    shim = _PooledRequests(session, requests)  # type: ignore[arg-type]

    shim.request(method="POST", url="https://rerank.example/api", headers={})

    assert session.posts == [], "a `request` must not be sent as a `post`"


def test_a_request_call_carries_the_trace_context(spans: InMemorySpanExporter) -> None:
    """Safe despite the signing, and worth having: VikingDB traces too.

    `SignerV4` signs Content-Type, Content-Md5, Host and X-* only, and names
    exactly those in SignedHeaders. `traceparent` can never be in that set, so
    a verifier ignores it.
    """
    import requests

    shim = _PooledRequests(FakeSession(), requests)  # type: ignore[arg-type]
    headers: dict[str, str] = {"X-Date": "20260908T000000Z"}

    with _tracer().start_as_current_span("ov_ext.retrieval.parent"):
        shim.request(method="POST", url="https://rerank.example/api", headers=headers)

    assert "traceparent" in headers
    assert headers["X-Date"] == "20260908T000000Z", "signed headers must survive"


def test_the_volcengine_client_is_patched_too() -> None:
    """Both providers, not just the one the log happened to name."""
    volcengine = importlib.import_module("openviking.models.rerank.volcengine_rerank")
    original = volcengine.requests

    install_pooled_rerank()

    assert volcengine.requests is not original
    assert isinstance(volcengine.requests, _PooledRequests)


def test_a_nested_dashscope_body_still_reports_its_batch_size(
    spans: InMemorySpanExporter,
) -> None:
    """DashScope-native endpoints nest `documents` under `input`."""
    import requests

    shim = _PooledRequests(FakeSession(), requests)  # type: ignore[arg-type]

    shim.post(
        url="https://x/api/v1/services/rerank",
        headers={},
        json={"model": "m", "input": {"query": "q", "documents": ["a", "b", "c"]}},
    )

    call = next(
        s for s in spans.get_finished_spans() if s.name == "ov_ext.retrieval.rerank_call"
    )
    assert call.attributes is not None
    assert call.attributes["ov_ext.retrieval.documents"] == 3


def test_the_shim_falls_through_to_the_real_module() -> None:
    """Anything the clients reach for that is not `post` still works."""
    import requests

    shim = _PooledRequests(FakeSession(), requests)  # type: ignore[arg-type]

    assert shim.exceptions is requests.exceptions


def test_installing_replaces_the_clients_requests_module() -> None:
    """The patch lands where the rerank client actually calls the network."""
    module = importlib.import_module(_OPENAI_RERANK)
    original = module.requests

    assert install_pooled_rerank() is True

    assert module.requests is not original
    assert isinstance(module.requests, _PooledRequests)


def test_installing_twice_does_not_stack() -> None:
    """A second install would otherwise wrap the shim in another shim."""
    assert install_pooled_rerank() is True
    module = importlib.import_module(_OPENAI_RERANK)
    first = module.requests

    assert install_pooled_rerank() is False

    assert module.requests is first


def test_the_packages_uninstall_also_unpools() -> None:
    """``uninstall()`` has to undo everything ``install()`` did.

    Tested through the public function rather than the pooling one it calls:
    the tests around it all reach for `uninstall_pooled_rerank` directly, so
    nothing otherwise notices if `uninstall()` stops calling it and leaves both
    modules patched with the session open.
    """
    from ov_ext.retrieval.patch import install, uninstall

    module = importlib.import_module(_OPENAI_RERANK)
    original = module.requests
    install()
    assert isinstance(module.requests, _PooledRequests), "nothing to undo otherwise"

    uninstall()

    assert module.requests is original


def test_uninstalling_restores_the_real_module() -> None:
    """Process-global state, so it has to be reversible."""
    module = importlib.import_module(_OPENAI_RERANK)
    original = module.requests
    install_pooled_rerank()

    uninstall_pooled_rerank()

    assert module.requests is original


def test_uninstalling_closes_the_pooled_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise an install/uninstall/install cycle orphans open sockets.

    Which is exactly what a test suite does, so the leak would land here first.
    """
    import requests

    closed: list[str] = []
    original = requests.Session.close

    def close(self: requests.Session) -> None:
        closed.append("closed")
        original(self)

    monkeypatch.setattr(requests.Session, "close", close)
    install_pooled_rerank()

    uninstall_pooled_rerank()

    assert closed, "the pooled session's connections would stay open"


def test_install_turns_pooling_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only thing that makes this feature happen in a real server."""
    from ov_ext.retrieval.patch import install, uninstall

    monkeypatch.delenv("OV_RETRIEVAL_RERANK_POOLING", raising=False)
    module = importlib.import_module(_OPENAI_RERANK)
    try:
        install()
        assert isinstance(module.requests, _PooledRequests)
    finally:
        uninstall()


def test_install_honours_pooling_being_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A setting nobody can turn off is not a setting."""
    from ov_ext.retrieval.patch import install, uninstall

    monkeypatch.setenv("OV_RETRIEVAL_RERANK_POOLING", "false")
    module = importlib.import_module(_OPENAI_RERANK)
    original = module.requests
    try:
        install()
        assert module.requests is original
    finally:
        uninstall()


@pytest.mark.asyncio
async def test_the_default_setting_leaves_reranking_uncapped(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """``rerank_max_calls=0`` must mean no ceiling, not a ceiling of zero.

    Zero is the shipped default, so reading it literally would stop every
    deployment reranking at all -- silently, since candidates would simply keep
    their vector scores. Asserted through ``retrieve``, because the translation
    happens there.
    """
    seen: list[int | None] = []

    async def base_retrieve(
        self: Any, query: Any, request_ctx: Any, limit: int = 5, **kwargs: Any
    ) -> Any:
        seen.append(_rerank_budget.get())
        return QueryResult(query=query, matched_contexts=[], searched_directories=[])

    monkeypatch.setattr(HierarchicalRetriever, "retrieve", base_retrieve)

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings()
            self.vector_store = None

    assert _Stubbed()._settings.rerank_max_calls == 0, "the default under test"
    await _Stubbed().retrieve(FakeQuery(), ctx=None, limit=2)

    assert seen == [None], "the default must not cap reranking"


@pytest.mark.asyncio
async def test_a_batch_of_blank_documents_costs_no_budget(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """The base class refuses these without calling the service.

    A subtree of directories with no abstract yet -- ordinary mid-backfill --
    would otherwise spend the whole ceiling before one real rerank happened.
    """
    retriever, reached = make_budgeted_retriever(monkeypatch, rerank_max_calls=2)
    token = _rerank_budget.set(2)
    try:
        blank = await retriever._rerank_scores("q", ["", "   "], [1.0, 2.0])
        assert _rerank_budget.get() == 2, "a refused batch must not be charged"
        await retriever._rerank_scores("q", ["real"], [3.0])
        assert _rerank_budget.get() == 1
    finally:
        _rerank_budget.reset(token)

    assert blank == [1.0, 2.0]
    assert reached == [1], "only the batch with real text may reach the service"


def make_budgeted_retriever(
    monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> tuple[HybridRetriever, list[int]]:
    """Return a retriever whose base rerank records its calls.

    Returns
    -------
    tuple
        The retriever, and a list receiving the document count of each call
        that reached the base implementation.
    """
    reached: list[int] = []

    async def base_rerank(
        self: Any, query: str, documents: list[str], fallback_scores: list[float]
    ) -> list[float]:
        reached.append(len(documents))
        return [9.0] * len(documents)

    monkeypatch.setattr(HierarchicalRetriever, "_rerank_scores", base_rerank)

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings(**overrides)

    return _Stubbed(), reached


@pytest.mark.asyncio
async def test_the_budget_stops_reranking_and_keeps_vector_scores(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """Past the ceiling, candidates keep the ordering they already had.

    That is the same degradation OpenViking applies when rerank fails, so the
    worst case is a ranking it already considers acceptable.
    """
    retriever, reached = make_budgeted_retriever(monkeypatch, rerank_max_calls=2)
    token = _rerank_budget.set(2)
    try:
        first = await retriever._rerank_scores("q", ["a"], [1.0])
        second = await retriever._rerank_scores("q", ["b"], [2.0])
        third = await retriever._rerank_scores("q", ["c"], [3.0])
    finally:
        _rerank_budget.reset(token)

    assert first == [9.0] and second == [9.0]
    assert third == [3.0], "past the budget the vector score must survive"
    assert len(reached) == 2, "the third call must not reach the rerank service"


@pytest.mark.asyncio
async def test_the_exhausted_budget_is_recorded_not_silent(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """A search that quietly stopped reranking looks like one with no reranker."""
    retriever, _ = make_budgeted_retriever(monkeypatch, rerank_max_calls=1)
    token = _rerank_budget.set(0)
    try:
        await retriever._rerank_scores("q", ["a"], [1.0])
    finally:
        _rerank_budget.reset(token)

    span = next(
        s for s in spans.get_finished_spans() if s.name == "ov_ext.retrieval.rerank"
    )
    assert span.attributes is not None
    assert span.attributes["ov_ext.retrieval.outcome"] == "rerank_budget_spent"


@pytest.mark.asyncio
async def test_no_budget_means_no_ceiling(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """The default must not change behaviour for anyone not asking for it."""
    retriever, reached = make_budgeted_retriever(monkeypatch, rerank_max_calls=0)

    assert _rerank_budget.get() is None
    for _ in range(5):
        await retriever._rerank_scores("q", ["a"], [1.0])

    assert len(reached) == 5


@pytest.mark.asyncio
async def test_retrieve_sets_the_budget_before_the_descent_and_clears_it_after(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """The setting only bites if it is in place while the descent runs.

    The descent lives inside ``super().retrieve``, so the budget has to be set
    before that call and gone after it. Asserting only the "after" half would
    pass just as well if the budget were never set at all.

    Clearing matters because one retriever serves every request and the loop
    reuses tasks: a counter left set would make the next search skip reranking
    for reasons this one caused.
    """
    seen: list[int | None] = []

    async def base_retrieve(
        self: Any, query: Any, request_ctx: Any, limit: int = 5, **kwargs: Any
    ) -> Any:
        seen.append(_rerank_budget.get())
        return QueryResult(query=query, matched_contexts=[], searched_directories=[])

    monkeypatch.setattr(HierarchicalRetriever, "retrieve", base_retrieve)

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings(rerank_max_calls=7)
            self.vector_store = None

    await _Stubbed().retrieve(FakeQuery(), ctx=None, limit=2)

    assert seen == [7], "the descent must run with the configured ceiling in place"
    assert _rerank_budget.get() is None


def make_recording_retriever(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scores: list[float] | None = None,
    **overrides: Any,
) -> tuple[HybridRetriever, list[tuple[list[str], list[float]]]]:
    """Return a retriever whose base rerank records what it was handed.

    ``make_budgeted_retriever`` records only how many documents arrived, which
    cannot tell a cap that sent the strongest candidates from one that sent the
    first few.

    Parameters
    ----------
    scores :
        Returned to the caller, truncated to the batch. Defaults to a constant,
        which is enough when the test is about what was sent rather than what
        came back.

    Returns
    -------
    tuple
        The retriever, and a list receiving ``(documents, fallback_scores)``
        for every call that reached the base implementation.
    """
    seen: list[tuple[list[str], list[float]]] = []

    async def base_rerank(
        self: Any, query: str, documents: list[str], fallback_scores: list[float]
    ) -> list[float]:
        seen.append((list(documents), list(fallback_scores)))
        return list(scores[: len(documents)]) if scores else [9.0] * len(documents)

    monkeypatch.setattr(HierarchicalRetriever, "_rerank_scores", base_rerank)

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings(**overrides)

    return _Stubbed(), seen


@pytest.mark.asyncio
async def test_the_document_cap_sends_the_strongest_candidates_in_batch_order(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """Two claims at once, because they are easy to get right separately.

    The cap must choose by vector score -- sending the first N would rerank
    whatever the store happened to return first. And it must then restore the
    batch's own order, so the request reads like an uncapped one. The fallback
    scores here rank ``d`` above ``b``, so score order and batch order differ
    and only one of them can pass.
    """
    retriever, seen = make_recording_retriever(monkeypatch, rerank_max_documents=2)

    await retriever._rerank_scores("q", ["a", "b", "c", "d"], [0.1, 0.7, 0.3, 0.9])

    sent, fallbacks = seen[0]
    assert sent == ["b", "d"], "the two strongest, in the batch's own order"
    assert fallbacks == [0.7, 0.9], "each document keeps its own vector score"


@pytest.mark.asyncio
async def test_candidates_past_the_document_cap_keep_their_vector_scores(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """The operator's decision: hold them back from the reranker, not from the pool.

    Dropping them, or flooring them below the reranked band, would push them
    under the retrieval threshold and out of the answer entirely. Keeping the
    vector score is the merge the base class already performs for documents it
    skips.
    """
    retriever, _ = make_recording_retriever(
        monkeypatch, scores=[5.0, 6.0], rerank_max_documents=2
    )

    result = await retriever._rerank_scores(
        "q", ["a", "b", "c", "d"], [0.1, 0.7, 0.3, 0.9]
    )

    assert result == [0.1, 5.0, 0.3, 6.0], (
        "reranked scores land on the chosen indexes; the rest keep theirs"
    )


@pytest.mark.asyncio
async def test_the_default_document_cap_sends_every_document(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """``rerank_max_documents=0`` must mean no cap, not a cap of zero.

    Zero is the shipped default, so reading it literally would stop every
    deployment reranking at all -- silently, since candidates would simply keep
    their vector scores.
    """
    retriever, seen = make_recording_retriever(monkeypatch)

    assert retriever._settings.rerank_max_documents == 0, "the default under test"
    await retriever._rerank_scores("q", ["a", "b", "c"], [0.1, 0.2, 0.3])

    assert seen[0][0] == ["a", "b", "c"], "the default must send everything"


@pytest.mark.asyncio
async def test_the_cap_spends_its_allowance_on_documents_that_carry_text(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """Blanks are dropped before the cap applies, not after.

    Capping first fills the allowance by vector score, which here is both
    blanks -- so the request goes out empty while the ceiling has already been
    charged for it. Found by adversarial review.
    """
    retriever, seen = make_recording_retriever(monkeypatch, rerank_max_documents=2)

    token = _rerank_budget.set(5)
    try:
        await retriever._rerank_scores("q", ["real text", "", "   "], [0.1, 0.9, 0.8])
        remaining = _rerank_budget.get()
    finally:
        _rerank_budget.reset(token)

    assert seen[0][0] == ["real text"], (
        "the allowance must go to text the service can read"
    )
    assert remaining == 4, "and the call it was charged for must have carried something"
