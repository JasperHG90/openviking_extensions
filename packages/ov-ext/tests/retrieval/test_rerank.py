"""Coverage for connection pooling and the per-search rerank document cap.

OpenViking's rerank client opens a fresh connection per call, and since 0.4.23
its one rerank per search scores the whole recalled pool -- which ov-ext
widens. Pooling cuts the handshake; the cap bounds what that call sends.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from openviking.retrieve.hierarchical_retriever import HierarchicalRetriever
from openviking.retrieve.rerank_scoring import RerankScores

from ov_ext.retrieval.config import HybridSettings
from ov_ext.observability import _tracer
from ov_ext.retrieval.rerank import (
    _PooledRequests,
    install_pooled_rerank,
    uninstall_pooled_rerank,
)
from ov_ext.retrieval.retriever import HybridRetriever

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


def make_recording_retriever(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scores: list[float] | None = None,
    **overrides: Any,
) -> tuple[HybridRetriever, list[tuple[list[str], list[float]]]]:
    """Return a retriever whose base rerank records what it was handed.

    The stub answers the way OpenViking's own ``rerank_scores`` does: a model
    score for every document carrying text, the fallback for a blank, and a
    ``reranked`` flag saying which is which.

    Parameters
    ----------
    scores :
        Model scores, consumed in order by the documents that carry text.
        Defaults to a constant, which is enough when the test is about what
        was sent rather than what came back.

    Returns
    -------
    tuple
        The retriever, and a list receiving ``(documents, fallback_scores)``
        for every call that reached the base implementation.
    """
    seen: list[tuple[list[str], list[float]]] = []

    async def base_rerank(
        self: Any, query: str, documents: list[str], fallback_scores: list[float]
    ) -> RerankScores:
        seen.append((list(documents), list(fallback_scores)))
        answers = iter(scores or [9.0] * len(documents))
        out = [
            next(answers) if text.strip() else fallback
            for text, fallback in zip(documents, fallback_scores, strict=True)
        ]
        return RerankScores(out, [bool(text.strip()) for text in documents])

    monkeypatch.setattr(HierarchicalRetriever, "_rerank_scores", base_rerank)

    class _Stubbed(HybridRetriever):
        def __init__(self) -> None:
            self._settings = HybridSettings(**overrides)

    return _Stubbed(), seen


@pytest.mark.asyncio
async def test_the_override_returns_what_the_base_retrieve_reads(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """OpenViking 0.5.3+ reads ``.scores`` and ``.reranked`` off the result.

    ov-ext 0.7 returned a plain list here, which made the base ``retrieve``
    raise ``AttributeError`` on every reranked search.
    """
    cases: list[dict[str, Any]] = [{}, {"rerank_max_documents": 1}]
    for overrides in cases:
        retriever, _ = make_recording_retriever(monkeypatch, **overrides)
        result = await retriever._rerank_scores("q", ["a", "b"], [0.1, 0.2])
        assert isinstance(result, RerankScores), overrides
        assert len(result.scores) == len(result.reranked) == 2


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
    """Held back from the reranker, not from the pool, and marked as such.

    Dropping them, or flooring them below the reranked band, would push them
    under the retrieval threshold and out of the answer entirely. Keeping the
    vector score, flagged as not reranked, is the base class's own treatment
    of a document the model skipped -- so the rerank threshold leaves them be.
    """
    retriever, _ = make_recording_retriever(
        monkeypatch, scores=[5.0, 6.0], rerank_max_documents=2
    )

    result = await retriever._rerank_scores(
        "q", ["a", "b", "c", "d"], [0.1, 0.7, 0.3, 0.9]
    )

    assert result.scores == [0.1, 5.0, 0.3, 6.0], (
        "reranked scores land on the chosen indexes; the rest keep theirs"
    )
    assert result.reranked == [False, True, False, True]


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
    result = await retriever._rerank_scores("q", ["a", "b", "c"], [0.1, 0.2, 0.3])

    assert seen[0][0] == ["a", "b", "c"], "the default must send everything"
    assert result.reranked == [True, True, True]


@pytest.mark.asyncio
async def test_the_cap_spends_its_allowance_on_documents_that_carry_text(
    monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """Blanks are dropped before the cap applies, not after.

    Capping first fills the allowance by vector score, which here is both
    blanks -- the base class never sends a blank, so the call would go out
    empty. Found by adversarial review.
    """
    retriever, seen = make_recording_retriever(monkeypatch, rerank_max_documents=2)

    result = await retriever._rerank_scores(
        "q", ["real text", "", "   "], [0.1, 0.9, 0.8]
    )

    assert seen[0][0] == ["real text"], (
        "the allowance must go to text the service can read"
    )
    assert result.scores == [9.0, 0.9, 0.8]
    assert result.reranked == [True, False, False]
