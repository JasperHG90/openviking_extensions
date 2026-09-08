"""Coverage for the tracing helpers and the spans hybrid retrieval emits.

The wiring tests below matter more than the helper tests. Every pass this
package adds degrades to "do nothing" rather than to an error, so a keyword leg
that raises on every query and one that ran and matched nothing look identical
from outside. These assert that a trace tells them apart.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ov_retrieval.observability import annotate, record_error, traced, traced_sync

# Marked per test rather than with a module-level `pytestmark`, and explicit
# rather than relying on `asyncio_mode = "auto"`: pytest reads that setting
# from this package's pyproject only when rootdir is this package, so a run
# from the workspace root would skip every async test here as unsupported. A
# module-level mark would fix that too, but it also lands on the sync tests
# below, which pytest-asyncio warns about.

# Absolute, not relative: this test package carries no `__init__.py`, so pytest
# imports each module top-level with the directory on sys.path.
#
# `restore_base_class` is imported for its effect as a fixture rather than to
# be called: it is autouse, and pytest finds it in this module's namespace, so
# the stub `make_retriever` puts on OpenViking's base class is undone here too.
from test_retriever import (  # noqa: F401
    FakeQuery,
    FakeStore,
    ctx,
    make_retriever,
    restore_base_class,
)


class Stub:
    """A traced object standing in for the retriever."""

    @traced("ov_retrieval.stub")
    async def work(self, value: int = 1) -> int:
        """Return ``value`` doubled, annotating what it saw."""
        annotate({"ov_retrieval.pairs": value})
        return value * 2

    @traced("ov_retrieval.degrades")
    async def degrades(self) -> list[str]:
        """Swallow a failure the way this package's real methods do."""
        try:
            raise RuntimeError("backend fell over")
        except RuntimeError as exc:
            record_error(exc, "keyword_search_failed")
            return []


@pytest.mark.asyncio
async def test_a_traced_method_opens_one_span_named_after_it(
    spans: InMemorySpanExporter,
) -> None:
    """The span carries the name given to the decorator, not the method's."""
    await Stub().work()

    assert [span.name for span in spans.get_finished_spans()] == ["ov_retrieval.stub"]


@pytest.mark.asyncio
async def test_a_traced_method_returns_what_it_would_have(
    spans: InMemorySpanExporter,
) -> None:
    """Tracing is transparent: same return value, same arguments."""
    assert await Stub().work(21) == 42


@pytest.mark.asyncio
async def test_annotate_adds_to_the_span_that_is_open(
    spans: InMemorySpanExporter,
) -> None:
    """Counts known only inside the method still land on its span."""
    await Stub().work(7)

    span = spans.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes["ov_retrieval.pairs"] == 7


def test_annotate_outside_a_span_does_nothing(spans: InMemorySpanExporter) -> None:
    """Safe to call unconditionally: no current span means no attribute, no error.

    This is the same code path taken in production when tracing is off, where
    the current span is the API's invalid one.
    """
    annotate({"ov_retrieval.pairs": 1})

    assert spans.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_a_swallowed_failure_still_marks_its_own_span_failed(
    spans: InMemorySpanExporter,
) -> None:
    """The whole point: nothing raised, and the trace still shows the failure."""
    assert await Stub().degrades() == []

    span = spans.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]
    assert span.attributes is not None
    assert span.attributes["ov_retrieval.outcome"] == "keyword_search_failed"


def test_traced_refuses_a_sync_method() -> None:
    """Wrapping a sync method would hand the caller a coroutine nobody awaits.

    Raised at decoration time, so the mistake surfaces on import.
    """
    with pytest.raises(TypeError, match="sync"):
        # Deliberately the wrong type: the point is the runtime guard, and
        # mypy correctly objects to the very call this test exists to make.
        @traced("ov_retrieval.wrong")  # type: ignore[arg-type]
        def plain(self: object) -> None:
            """Never decorated successfully."""


def test_traced_sync_refuses_an_async_function() -> None:
    """The mirror of the guard on ``traced``, which has one and is tested.

    Wrapping a coroutine in the sync wrapper would return the coroutine
    unawaited, so the call would appear to succeed and do nothing.
    """
    with pytest.raises(TypeError, match="async"):
        # mypy accepts this: a coroutine function satisfies `Callable[P, R]`
        # with R bound to the coroutine. Which is exactly why the guard has to
        # exist at runtime -- the type system will not catch this mistake.
        @traced_sync("ov_retrieval.wrong")
        async def coroutine() -> None:
            """Never decorated successfully."""


def test_traced_sync_opens_a_span_and_returns_the_value(
    spans: InMemorySpanExporter,
) -> None:
    """The rerank client's HTTP call is sync, and still has to be traced."""

    @traced_sync("ov_retrieval.sync_work")
    def work(value: int) -> int:
        """Double a number inside a span."""
        return value * 2

    assert work(21) == 42
    assert [s.name for s in spans.get_finished_spans()] == ["ov_retrieval.sync_work"]


@pytest.mark.asyncio
async def test_retrieve_records_the_shape_of_the_work(
    spans: InMemorySpanExporter,
) -> None:
    """The top span says what was asked for and what came back."""
    store = FakeStore(keyword_uris=["c", "a"])
    retriever = make_retriever(store, [ctx("a"), ctx("b"), ctx("c")], mmr_enabled=False)

    await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

    top = next(s for s in spans.get_finished_spans() if s.name == "ov_retrieval.retrieve")
    assert top.attributes is not None
    assert top.attributes["ov_retrieval.limit"] == 2
    assert top.attributes["ov_retrieval.pool"] == 8
    assert top.attributes["ov_retrieval.keyword_enabled"] is True
    assert top.attributes["ov_retrieval.candidates"] == 3
    assert top.attributes["ov_retrieval.results"] == 2


@pytest.mark.asyncio
async def test_a_single_candidate_still_reports_its_result_count(
    spans: InMemorySpanExporter,
) -> None:
    """The early return is still a retrieval, and reports what it returned.

    It leaves before either pass runs, which is exactly where an annotation is
    easy to forget.
    """
    store = FakeStore(keyword_uris=["a"])
    retriever = make_retriever(store, [ctx("a")])

    await retriever.retrieve(FakeQuery(), ctx=None, limit=5)

    top = next(s for s in spans.get_finished_spans() if s.name == "ov_retrieval.retrieve")
    assert top.attributes is not None
    assert top.attributes["ov_retrieval.outcome"] == "too_few_candidates"
    assert top.attributes["ov_retrieval.results"] == 1


@pytest.mark.asyncio
async def test_the_passes_nest_under_the_retrieval(
    spans: InMemorySpanExporter,
) -> None:
    """Each pass is a child span, so a trace shows where the time went."""
    store = FakeStore(keyword_uris=["c", "a"], similarity={("a", "c"): 0.9})
    retriever = make_retriever(store, [ctx("a"), ctx("b"), ctx("c")])

    await retriever.retrieve(FakeQuery(), ctx=None, limit=3)

    finished = {span.name: span for span in spans.get_finished_spans()}
    top = finished["ov_retrieval.retrieve"]
    assert {
        "ov_retrieval.fuse_keywords",
        "ov_retrieval.keyword_search",
        "ov_retrieval.diversify",
    } <= set(finished)
    top_context = top.get_span_context()
    assert top_context is not None
    for name in ("ov_retrieval.fuse_keywords", "ov_retrieval.diversify"):
        parent = finished[name].parent
        assert parent is not None
        assert parent.span_id == top_context.span_id


@pytest.mark.asyncio
async def test_a_failing_keyword_leg_is_visible_without_failing_the_retrieval(
    spans: InMemorySpanExporter,
) -> None:
    """A degraded answer is still an answer, and the trace says why it degraded.

    Without this the keyword leg raising on every query would be invisible: it
    is caught, logged at warning, and the vector ranking is returned.
    """
    store = FakeStore(keyword_uris=["a"], keyword_error=RuntimeError("boom"))
    retriever = make_retriever(store, [ctx("a"), ctx("b")], mmr_enabled=False)

    result = await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

    assert [m.uri for m in result.matched_contexts] == ["a", "b"]
    finished = {span.name: span for span in spans.get_finished_spans()}
    keyword = finished["ov_retrieval.keyword_search"]
    assert keyword.status.status_code is StatusCode.ERROR
    assert keyword.attributes is not None
    assert keyword.attributes["ov_retrieval.outcome"] == "keyword_search_failed"
    # The caller got results, so the retrieval itself did not fail.
    assert finished["ov_retrieval.retrieve"].status.status_code is StatusCode.UNSET


@pytest.mark.asyncio
async def test_a_backend_without_keyword_search_says_so(
    spans: InMemorySpanExporter,
) -> None:
    """Distinguishes "cannot" from "found nothing", which logs alone did not."""

    class Bare:
        """A vector store offering neither scoping nor keyword search."""

    retriever = make_retriever(Bare(), [ctx("a"), ctx("b")], mmr_enabled=False)  # type: ignore[arg-type]

    await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

    keyword = next(
        s for s in spans.get_finished_spans() if s.name == "ov_retrieval.keyword_search"
    )
    assert keyword.attributes is not None
    assert keyword.attributes["ov_retrieval.outcome"] == "backend_lacks_keyword_search"


@pytest.mark.asyncio
async def test_diversity_without_a_similarity_signal_says_so(
    spans: InMemorySpanExporter,
) -> None:
    """MMR over an empty matrix would silently reproduce the input order."""
    store = FakeStore(keyword_uris=[])
    retriever = make_retriever(
        store, [ctx("a"), ctx("b")], keyword_enabled=False, mmr_lambda=0.5
    )

    await retriever.retrieve(FakeQuery(), ctx=None, limit=2)

    diversify = next(
        s for s in spans.get_finished_spans() if s.name == "ov_retrieval.diversify"
    )
    assert diversify.attributes is not None
    assert diversify.attributes["ov_retrieval.outcome"] == "no_similarity_signal"
