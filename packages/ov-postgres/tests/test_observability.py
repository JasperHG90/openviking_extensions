"""Coverage for the tracing helpers and the spans the backend actually emits.

The unit tests below exercise ``traced`` and ``annotate`` against a stub, since
the helpers care about method shape rather than about PostgreSQL. The
integration class at the bottom is the one that proves the wiring: that a real
``search_by_vector`` against a real database produces a span naming the real
table.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ov_postgres.observability import annotate, traced


class Stub:
    """A traced object standing in for a collection."""

    def __init__(self, attributes: dict[str, Any] | None = None) -> None:
        if attributes is not None:
            self._span_attributes = attributes

    @traced("ov_postgres.stub")
    def work(self, value: int = 1) -> int:
        """Return ``value`` doubled, annotating what it saw."""
        annotate({"ov_postgres.rows": value})
        return value * 2

    @traced("ov_postgres.explodes")
    def explodes(self) -> None:
        """Raise, so the span's error handling can be observed."""
        raise ValueError("no")


def test_a_traced_method_opens_one_span_named_after_it(
    spans: InMemorySpanExporter,
) -> None:
    """The span carries the name given to the decorator, not the method's."""
    Stub().work()

    finished = spans.get_finished_spans()
    assert [span.name for span in finished] == ["ov_postgres.stub"]


def test_a_traced_method_returns_what_it_would_have(
    spans: InMemorySpanExporter,
) -> None:
    """Tracing is transparent: same return value, same arguments."""
    assert Stub().work(21) == 42


def test_a_traced_method_keeps_its_identity(spans: InMemorySpanExporter) -> None:
    """``functools.wraps`` keeps the name and docstring a reader expects."""
    assert Stub.work.__name__ == "work"
    assert Stub.work.__doc__ is not None
    assert "doubled" in Stub.work.__doc__


def test_the_span_carries_the_instances_attributes(
    spans: InMemorySpanExporter,
) -> None:
    """Schema and table reach every span without the method repeating them."""
    Stub({"db.namespace": "public", "db.collection.name": "ov_context"}).work()

    span = spans.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes["db.namespace"] == "public"
    assert span.attributes["db.collection.name"] == "ov_context"


def test_an_instance_without_attributes_is_still_traced(
    spans: InMemorySpanExporter,
) -> None:
    """``_span_attributes`` is optional; the adapter has none."""
    Stub().work()

    assert len(spans.get_finished_spans()) == 1


def test_annotate_adds_to_the_span_that_is_open(
    spans: InMemorySpanExporter,
) -> None:
    """Counts known only inside the method still land on its span."""
    Stub().work(7)

    span = spans.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes["ov_postgres.rows"] == 7


def test_annotate_outside_a_span_does_nothing(spans: InMemorySpanExporter) -> None:
    """Safe to call unconditionally: no current span means no attribute, no error.

    This is the same code path taken in production when tracing is off, where
    the current span is the API's invalid one.
    """
    annotate({"ov_postgres.rows": 1})

    assert spans.get_finished_spans() == ()


def test_an_exception_marks_the_span_failed_and_still_propagates(
    spans: InMemorySpanExporter,
) -> None:
    """A raising method is recorded, not swallowed."""
    with pytest.raises(ValueError):
        Stub().explodes()

    span = spans.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]


def test_traced_refuses_an_async_method() -> None:
    """Wrapping a coroutine here would hand the caller one nobody awaits.

    Raised at decoration time, so the mistake surfaces on import.
    """
    with pytest.raises(TypeError, match="async"):

        @traced("ov_postgres.wrong")
        async def coroutine(self: object) -> None:
            """Never decorated successfully."""


def traced_collection_spans() -> dict[str, bool]:
    """Return each traced collection span, and whether it names its operation.

    Read from the source rather than by running anything, so a method added
    later is covered the moment it is decorated -- including one whose
    integration test nobody wrote.
    """
    import ast
    import pathlib

    import ov_postgres.collection as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    found: dict[str, bool] = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            decorators = [
                d
                for d in fn.decorator_list
                if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "traced"
            ]
            if not decorators:
                continue
            name = decorators[0].args[0]
            assert isinstance(name, ast.Constant)
            found[str(name.value)] = any(
                isinstance(key, ast.Constant) and key.value == "db.operation.name"
                for node in ast.walk(fn)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "annotate"
                for arg in node.args
                if isinstance(arg, ast.Dict)
                for key in arg.keys
                if key is not None
            )
    return found


def test_every_public_method_that_runs_sql_is_traced() -> None:
    """The README's claim, enforced: SQL never runs outside a span.

    Derived from what each method *does* -- whether it reaches the driver --
    rather than from which methods carry the decorator. An expectation read off
    the decorators would shrink along with them, so deleting a ``@traced``
    would keep passing.
    """
    import ast
    import pathlib

    import ov_postgres.adapter
    import ov_postgres.collection

    # The adapter as well as the collection: creating a collection's table is
    # the most consequential DDL this package runs, and it happens here.
    by_class: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for module in (ov_postgres.collection, ov_postgres.adapter):
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        # Module-level functions count too: SQL reached through one of those
        # would otherwise be invisible to this walk.
        module_level = {
            fn.name: fn
            for fn in tree.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # Per class, not a flat map by name: two classes define
        # `get_meta_data`, and `_IndexHandle`'s -- which returns a dict and
        # runs no SQL -- would otherwise mask the collection's, hiding a lost
        # decorator.
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            by_class[f"{module.__name__}.{cls.name}"] = {
                **module_level,
                **{
                    fn.name: fn
                    for fn in cls.body
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                },
            }
    # Driver verbs, plus this package's own SQL funnel.
    driver = {"execute", "executemany", "connection", "connect", "cursor", "_execute"}

    def is_traced(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(
            isinstance(d, ast.Call) and getattr(d.func, "id", "") == "traced"
            for d in fn.decorator_list
        )

    def runs_untraced_sql(
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
        seen: frozenset[str] = frozenset(),
    ) -> bool:
        """Whether ``fn`` reaches the driver without a span already open."""
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            # `self.method()` gives an Attribute, a module-level helper a Name.
            if isinstance(called, ast.Attribute):
                attr: str | None = called.attr
            elif isinstance(called, ast.Name):
                attr = called.id
            else:
                attr = None
            if attr in driver:
                return True
            if attr is None or attr not in methods or attr in seen:
                continue
            # A traced callee opens its own span, so its SQL is covered and
            # does not count against this caller. `get_index` is the live case:
            # it runs no SQL itself and delegates to `get_index_meta_data`.
            if is_traced(methods[attr]):
                continue
            if runs_untraced_sql(methods[attr], methods, seen | {attr}):
                return True
        return False

    untraced = sorted(
        f"{cls}.{name}"
        for cls, methods in by_class.items()
        for name, fn in methods.items()
        if not name.startswith("_")
        and not is_traced(fn)
        and runs_untraced_sql(fn, methods)
    )
    assert untraced == [], f"public methods running SQL outside a span: {untraced}"


def test_every_traced_method_names_the_operation_it_performs() -> None:
    """No traced method may ship without a ``db.operation.name``.

    What the *value* is checks out against real SQL in
    ``test_every_span_declares_the_sql_it_actually_issues``.
    """
    spans_found = traced_collection_spans()

    assert spans_found, "found no traced methods at all; the parser has drifted"
    missing = sorted(name for name, declares in spans_found.items() if not declares)
    assert missing == [], f"traced but no db.operation.name: {missing}"


@pytest.fixture
def issued_sql(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Record the statements each span issues, keyed by span name.

    Spies on the driver rather than on this package, so what is recorded is
    what PostgreSQL was actually sent. A span claiming an operation it never
    performs is otherwise undetectable: the attribute is written by hand and
    nothing checks it against the SQL.
    """
    import psycopg
    from opentelemetry import trace as otel_trace

    issued: dict[str, list[str]] = {}
    original_execute = psycopg.Cursor.execute
    original_executemany = psycopg.Cursor.executemany

    def note(cursor: Any, query: Any) -> None:
        name = getattr(otel_trace.get_current_span(), "name", None)
        if name is None:
            return
        text = query if isinstance(query, str) else query.as_string(cursor)
        issued.setdefault(name, []).append(" ".join(str(text).split()).upper())

    def execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        note(self, query)
        return original_execute(self, query, *args, **kwargs)

    def executemany(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        note(self, query)
        return original_executemany(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    monkeypatch.setattr(psycopg.Cursor, "executemany", executemany)
    return issued


@pytest.mark.integration
class TestSpansFromRealQueries:
    """What a real query against a real database actually emits."""

    def test_every_span_declares_the_sql_it_actually_issues(
        self,
        dsn: str,
        test_schema: str,
        spans: InMemorySpanExporter,
        issued_sql: dict[str, list[str]],
    ) -> None:
        """``db.operation.name`` must match a statement the span really ran.

        Exercises every traced collection method and compares what each span
        claims against what the driver was handed. Written after two spans
        shipped lying: ``drop_index`` said DROP INDEX while only deleting a
        registry row, and ``ensure_indexes`` said CREATE INDEX on the
        steady-state path, where it only reads the catalog.
        """
        from .test_backends import build
        from .test_integration import vec

        adapter = build(dsn, test_schema)
        try:
            collection = adapter._pg_collection
            assert collection is not None
            spans.clear()
            issued_sql.clear()

            collection.get_meta_data()
            collection.update(fields={"probe": 1})
            collection.list_indexes()
            collection.has_index("default")
            collection.get_index_meta_data("default")
            # Straight at the collection, so the records carry the primary key
            # the schema declares -- `id` -- rather than the adapter's shape.
            collection.upsert_data(
                [
                    {"id": "a", "uri": "/a", "name": "alpha", "vector": vec(1)},
                    {"id": "b", "uri": "/b", "name": "beta", "vector": vec(2)},
                ]
            )
            collection.update_data([{"id": "a", "name": "alpha renamed"}])
            collection.fetch_data(["a"])
            collection.search_by_vector("default", dense_vector=vec(1), limit=2)
            collection.search_by_id("default", "a", limit=2)
            collection.search_by_keywords("default", query="alpha", limit=2)
            collection.search_by_random("default", limit=2)
            collection.search_by_scalar("default", field="uri", limit=2)
            collection.aggregate_data("default", op="count")
            collection.aggregate_data("default", op="count", field="name")
            collection.pairwise_similarity(["a", "b"])
            collection.backfill_defaults()
            collection.ensure_indexes()
            collection.create_index("probe_index", {})
            collection.update_index("probe_index", description="probe")
            collection.drop_index("probe_index")
            collection.delete_data(["/a"])
            collection.delete_all_data()
            collection.drop()
        finally:
            adapter.close()

        declared = {
            span.name: span.attributes["db.operation.name"]
            for span in spans.get_finished_spans()
            if span.attributes and "db.operation.name" in span.attributes
        }
        # Exactly the traced methods, no fewer. An equality rather than a
        # threshold so that removing a `@traced` decorator, or an `annotate`
        # that names the operation, fails here instead of quietly shrinking
        # what a trace shows. It also fails when a method is added and this
        # scenario is not extended to exercise it.
        assert set(declared) == set(traced_collection_spans())

        def performs(statement: str, operation: str) -> bool:
            """Whether ``statement`` carries out ``operation``.

            A read-only CTE counts as SELECT: ``pairwise_similarity`` builds
            its matrix with ``WITH reps AS (SELECT ...) SELECT ...``, which is
            a query however it starts. A CTE containing a write is *not*
            aliased, so the day someone adds ``WITH gone AS (DELETE ...)`` it
            has to be labelled by what it does rather than relabelled SELECT.
            """
            if statement.startswith("WITH ") and not any(
                verb in statement for verb in ("DELETE", "INSERT", "UPDATE")
            ):
                statement = "SELECT"
            return statement.startswith(operation)

        lying = {
            name: (operation, issued_sql.get(name, []))
            for name, operation in declared.items()
            if not any(
                performs(statement, str(operation))
                for statement in issued_sql.get(name, [])
            )
        }
        assert lying == {}, f"span declares an operation it never issued: {lying}"

        # Pinned, because "issued *some* statement of this kind" is too weak on
        # its own: a method issuing several kinds could declare any of them.
        # `drop` really does run DELETE before DROP TABLE, and `create_index` a
        # SELECT after the CREATE -- so only an exact table catches a swap
        # between two verbs the method genuinely uses.
        assert declared == {
            "ov_postgres.get_meta_data": "SELECT",
            "ov_postgres.update": "UPDATE",
            "ov_postgres.list_indexes": "SELECT",
            "ov_postgres.has_index": "SELECT",
            "ov_postgres.get_index_meta_data": "SELECT",
            "ov_postgres.upsert_data": "INSERT",
            "ov_postgres.update_data": "UPDATE",
            "ov_postgres.fetch_data": "SELECT",
            "ov_postgres.search_by_vector": "SELECT",
            "ov_postgres.search_by_id": "SELECT",
            "ov_postgres.search_by_keywords": "SELECT",
            "ov_postgres.search_by_random": "SELECT",
            "ov_postgres.search_by_scalar": "SELECT",
            "ov_postgres.aggregate_data": "SELECT",
            "ov_postgres.pairwise_similarity": "SELECT",
            "ov_postgres.backfill_defaults": "UPDATE",
            # Reads the catalog and builds nothing on this path.
            "ov_postgres.ensure_indexes": "SELECT",
            "ov_postgres.create_index": "CREATE INDEX",
            "ov_postgres.update_index": "UPDATE",
            # Forgets the registry row; the physical index is left alone.
            "ov_postgres.drop_index": "DELETE",
            "ov_postgres.delete_data": "DELETE",
            "ov_postgres.delete_all_data": "TRUNCATE",
            "ov_postgres.drop": "DROP TABLE",
        }

    def test_a_vector_search_names_the_table_and_counts_the_rows(
        self, dsn: str, test_schema: str, spans: InMemorySpanExporter
    ) -> None:
        """The span a search emits identifies the table and what came back."""
        from .test_backends import build
        from .test_integration import vec

        adapter = build(dsn, test_schema)
        try:
            adapter.upsert(
                [
                    {"uri": "/a", "name": "alpha", "vector": vec(1)},
                    {"uri": "/b", "name": "beta", "vector": vec(2)},
                ]
            )
            spans.clear()
            collection = adapter._pg_collection
            assert collection is not None
            collection.search_by_vector("default", dense_vector=vec(1), limit=2)
        finally:
            adapter.close()

        search = [
            span
            for span in spans.get_finished_spans()
            if span.name == "ov_postgres.search_by_vector"
        ]
        assert len(search) == 1
        attributes = search[0].attributes
        assert attributes is not None
        assert attributes["db.system.name"] == "postgresql"
        assert attributes["db.namespace"] == test_schema
        assert attributes["db.collection.name"] == "ov_context"
        assert attributes["db.operation.name"] == "SELECT"
        assert attributes["ov_postgres.limit"] == 2
        assert attributes["ov_postgres.dense"] is True
        assert attributes["ov_postgres.rows"] == 2

    def test_an_upsert_counts_the_records_it_was_given(
        self, dsn: str, test_schema: str, spans: InMemorySpanExporter
    ) -> None:
        """A write span says how much was written."""
        from .test_backends import build
        from .test_integration import vec

        adapter = build(dsn, test_schema)
        try:
            spans.clear()
            adapter.upsert(
                [{"uri": f"/{i}", "name": str(i), "vector": vec(1)} for i in range(3)]
            )
        finally:
            adapter.close()

        upserts = [
            span
            for span in spans.get_finished_spans()
            if span.name == "ov_postgres.upsert_data"
        ]
        assert len(upserts) == 1
        attributes = upserts[0].attributes
        assert attributes is not None
        assert attributes["db.operation.name"] == "INSERT"
        assert attributes["ov_postgres.records"] == 3
