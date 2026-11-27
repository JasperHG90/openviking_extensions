"""End-to-end tests against a live PostgreSQL + pgvector.

Run with::

    OV_POSTGRES_TEST_DSN=postgresql://localhost/openviking_test \
        .venv/bin/python -m pytest tests/test_integration.py -v

The most valuable test here is ``test_filter_semantics_match_reference``: it
generates random records and random filters, evaluates each filter both in
PostgreSQL (through the adapter) and in OpenViking's own local backend (see
``tests/reference.py``), and asserts the two agree on every row.  That is what
pins this backend to native behaviour rather than to my reading of it.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig
from psycopg import sql

from ov_postgres.adapter import PgVectorCollectionAdapter
from ov_postgres.collection import NO_VECTOR_SCORE

from .reference import LocalReference, engine_answer

pytestmark = pytest.mark.integration

# `CollectionAdapter.query` sizes the random vector it builds for a filter-only
# query from the collection's own `Dim`, so every schema here declares this.
DIM = 8

META: dict[str, Any] = {
    "CollectionName": "context",
    "Description": "test collection",
    "Fields": [
        {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
        {"FieldName": "uri", "FieldType": "path"},
        {"FieldName": "context_type", "FieldType": "string"},
        {"FieldName": "name", "FieldType": "string"},
        {"FieldName": "description", "FieldType": "string"},
        {"FieldName": "level", "FieldType": "int64"},
        {"FieldName": "active_count", "FieldType": "int64"},
        {"FieldName": "created_at", "FieldType": "date_time"},
        # `CollectionAdapter.delete(filter=...)` pages through matches ordered
        # by `updated_at`, as the real context schema allows.
        {"FieldName": "updated_at", "FieldType": "date_time"},
        {"FieldName": "search_tags", "FieldType": "list<string>"},
        {"FieldName": "vector", "FieldType": "vector", "Dim": DIM},
        {"FieldName": "sparse_vector", "FieldType": "sparse_vector"},
    ],
    "ScalarIndex": [
        "uri",
        "context_type",
        "name",
        "level",
        "active_count",
        "created_at",
        "search_tags",
    ],
}


def make_adapter(dsn: str, schema: str, **params: object) -> PgVectorCollectionAdapter:
    """Build an adapter pointed at a throwaway schema."""
    config = VectorDBBackendConfig(
        backend="ov_postgres.adapter.PgVectorCollectionAdapter",
        name="context",
        index_name="default",
        distance_metric="cosine",
        custom_params={"dsn": dsn, "schema": schema, **params},
    )
    return PgVectorCollectionAdapter.from_config(config)


@pytest.fixture
def adapter(dsn: str, test_schema: str) -> Iterator[PgVectorCollectionAdapter]:
    """Yield an adapter with the context collection already created."""
    inst = make_adapter(dsn, test_schema)
    inst.create_collection(
        "context", META, distance="cosine", sparse_weight=0.0, index_name="default"
    )
    try:
        yield inst
    finally:
        inst.close()


def vec(seed: int) -> list[float]:
    """Return a deterministic pseudo-random vector for the given seed."""
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(DIM)]


def test_create_is_idempotent_and_reports_existence(dsn: str, test_schema: str) -> None:
    """Creating an existing collection is a no-op, not an error."""
    inst = make_adapter(dsn, test_schema)
    assert inst.collection_exists() is False
    assert (
        inst.create_collection(
            "context", META, distance="cosine", sparse_weight=0.0, index_name="default"
        )
        is True
    )
    assert inst.collection_exists() is True
    # Second call must be a no-op, not an error.
    assert (
        inst.create_collection(
            "context", META, distance="cosine", sparse_weight=0.0, index_name="default"
        )
        is False
    )
    inst.close()


def test_reopen_finds_existing_collection(dsn: str, test_schema: str) -> None:
    """A fresh adapter binds to a collection created by an earlier one."""
    first = make_adapter(dsn, test_schema)
    first.create_collection(
        "context", META, distance="cosine", sparse_weight=0.0, index_name="default"
    )
    first.upsert({"id": "a", "uri": "viking://x", "vector": vec(1)})
    first.close()

    second = make_adapter(dsn, test_schema)
    assert second.collection_exists() is True
    assert len(second.get(["a"])) == 1
    second.close()


def test_drop_collection(adapter: PgVectorCollectionAdapter) -> None:
    """Dropping removes the collection and reports it gone."""
    adapter.upsert({"id": "a", "vector": vec(1)})
    assert adapter.drop_collection() is True
    assert adapter.collection_exists() is False


def test_get_collection_info_roundtrips_meta(adapter: PgVectorCollectionAdapter) -> None:
    """Stored metadata comes back exactly as it was declared."""
    info = adapter.get_collection_info()
    assert info["CollectionName"] == "context"
    assert len(info["Fields"]) == len(META["Fields"])


def test_index_registry(adapter: PgVectorCollectionAdapter) -> None:
    """Index bundles are listed, found, and readable by name."""
    coll = adapter.get_collection()
    assert coll.list_indexes() == ["default"]
    assert coll.has_index("default") is True
    assert coll.has_index("nope") is False
    assert coll.get_index_meta_data("default")["IndexName"] == "default"


def test_upsert_get_roundtrip(adapter: PgVectorCollectionAdapter) -> None:
    """Written fields read back with their types intact."""
    adapter.upsert(
        {
            "id": "a",
            "uri": "viking://user/default/notes",
            "name": "note",
            "level": 2,
            "created_at": "2026-03-22T08:39:45Z",
            "search_tags": ["x", "y"],
            "vector": vec(1),
        }
    )
    records = adapter.get(["a"])
    assert len(records) == 1
    record = records[0]
    assert record["id"] == "a"
    assert record["name"] == "note"
    assert record["level"] == 2
    assert record["search_tags"] == ["x", "y"]
    # URIs are stored path-style and decoded back on read.
    assert record["uri"] == "viking://user/default/notes"


def test_upsert_generates_ids_when_absent(adapter: PgVectorCollectionAdapter) -> None:
    """A record with no primary key is assigned one."""
    ids = adapter.upsert([{"name": "x", "vector": vec(1)}])
    assert len(ids) == 1 and ids[0]
    assert adapter.get(ids)[0]["name"] == "x"


def test_upsert_updates_existing_row(adapter: PgVectorCollectionAdapter) -> None:
    """Upserting a known key replaces rather than inserts."""
    adapter.upsert({"id": "a", "name": "before", "vector": vec(1)})
    adapter.upsert({"id": "a", "name": "after", "vector": vec(2)})
    assert adapter.get(["a"])[0]["name"] == "after"
    assert adapter.count() == 1


def test_unknown_fields_survive_in_extra(adapter: PgVectorCollectionAdapter) -> None:
    """Fields outside the schema round-trip through the JSONB column."""
    adapter.upsert({"id": "a", "custom_thing": {"k": 1}, "vector": vec(1)})
    assert adapter.get(["a"])[0]["custom_thing"] == {"k": 1}


def test_partial_update_leaves_other_columns(adapter: PgVectorCollectionAdapter) -> None:
    """``update_data`` writes only the columns it was given."""
    adapter.upsert({"id": "a", "name": "keep", "level": 1, "vector": vec(1)})
    adapter.update_data([{"id": "a", "level": 9}])
    record = adapter.get(["a"])[0]
    assert record["level"] == 9
    assert record["name"] == "keep"


def test_fetch_reports_missing_ids(adapter: PgVectorCollectionAdapter) -> None:
    """Keys that match nothing are reported back separately."""
    adapter.upsert({"id": "a", "vector": vec(1)})
    result = adapter.get_collection().fetch_data(["a", "ghost"])
    assert [item.id for item in result.items] == ["a"]
    assert result.ids_not_exist == ["ghost"]


def test_delete_by_id_and_by_filter(adapter: PgVectorCollectionAdapter) -> None:
    """Rows delete by explicit key and by filter."""
    adapter.upsert(
        [
            {"id": "a", "level": 1, "vector": vec(1)},
            {"id": "b", "level": 2, "vector": vec(2)},
            {"id": "c", "level": 2, "vector": vec(3)},
        ]
    )
    assert adapter.delete(ids=["a"]) == 1
    assert adapter.count() == 2
    assert adapter.delete(filter={"op": "must", "field": "level", "conds": [2]}) == 2
    assert adapter.count() == 0


def test_clear(adapter: PgVectorCollectionAdapter) -> None:
    """Clearing empties the table without dropping it."""
    adapter.upsert([{"id": str(i), "vector": vec(i)} for i in range(5)])
    assert adapter.clear() is True
    assert adapter.count() == 0


def test_vector_search_orders_by_similarity(adapter: PgVectorCollectionAdapter) -> None:
    """Nearer vectors score higher and rank first."""
    target = [1.0] + [0.0] * (DIM - 1)
    near = [0.99, 0.01] + [0.0] * (DIM - 2)
    far = [-1.0] + [0.0] * (DIM - 1)
    adapter.upsert(
        [
            {"id": "near", "vector": near},
            {"id": "far", "vector": far},
        ]
    )
    results = adapter.query(query_vector=target, limit=10)
    assert [r["id"] for r in results] == ["near", "far"]
    assert results[0]["_score"] > results[1]["_score"]


def test_vector_search_respects_filter(adapter: PgVectorCollectionAdapter) -> None:
    """A filter restricts the candidate set before ranking."""
    adapter.upsert(
        [
            {"id": "a", "level": 1, "vector": vec(1)},
            {"id": "b", "level": 2, "vector": vec(2)},
        ]
    )
    results = adapter.query(
        query_vector=vec(1),
        filter={"op": "must", "field": "level", "conds": [2]},
        limit=10,
    )
    assert [r["id"] for r in results] == ["b"]


def test_limit_and_offset(adapter: PgVectorCollectionAdapter) -> None:
    """Paging returns disjoint, ordered pages."""
    adapter.upsert([{"id": f"r{i}", "level": i, "vector": vec(i)} for i in range(10)])
    page1 = adapter.query(order_by="level", order_desc=False, limit=3, offset=0)
    page2 = adapter.query(order_by="level", order_desc=False, limit=3, offset=3)
    assert [r["id"] for r in page1] == ["r0", "r1", "r2"]
    assert [r["id"] for r in page2] == ["r3", "r4", "r5"]


def test_scalar_ordering_both_directions(adapter: PgVectorCollectionAdapter) -> None:
    """Scalar sort honours ascending and descending."""
    adapter.upsert([{"id": f"r{i}", "level": i, "vector": vec(i)} for i in range(4)])
    asc = adapter.query(order_by="level", order_desc=False, limit=10)
    desc = adapter.query(order_by="level", order_desc=True, limit=10)
    assert [r["id"] for r in asc] == ["r0", "r1", "r2", "r3"]
    assert [r["id"] for r in desc] == ["r3", "r2", "r1", "r0"]


def test_output_fields_projection(adapter: PgVectorCollectionAdapter) -> None:
    """Only the requested columns come back."""
    adapter.upsert({"id": "a", "name": "n", "description": "d", "vector": vec(1)})
    record = adapter.query(query_vector=vec(1), output_fields=["name"], limit=1)[0]
    assert record["name"] == "n"
    assert "description" not in record


def test_keyword_search(adapter: PgVectorCollectionAdapter) -> None:
    """Full-text search finds a row by a word in its name."""
    adapter.upsert(
        [
            {"id": "a", "name": "postgres vector search", "vector": vec(1)},
            {"id": "b", "name": "completely unrelated", "vector": vec(2)},
        ]
    )
    results = adapter.search_by_keywords(query="postgres", limit=10)
    assert [r["id"] for r in results] == ["a"]


def test_keyword_search_covers_array_fields(adapter: PgVectorCollectionAdapter) -> None:
    """Array text fields are searchable too."""
    adapter.upsert({"id": "a", "search_tags": ["kubernetes"], "vector": vec(1)})
    assert [r["id"] for r in adapter.search_by_keywords(keywords=["kubernetes"])] == ["a"]


def test_keyword_search_searches_only_the_fields_asked_for(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """``fields`` narrows the search, as on OpenViking's other keyword backends.

    OpenViking's own keyword callers send ``mode="bm25", fields=["content"]``:
    a match in another column is not a match in ``content``.
    """
    adapter.upsert(
        [
            {"id": "named", "name": "postgres vector search", "vector": vec(1)},
            {"id": "described", "description": "postgres replication", "vector": vec(2)},
            {"id": "other", "name": "completely unrelated", "vector": vec(3)},
        ]
    )
    everywhere = adapter.search_by_keywords(query="postgres", limit=10, mode="bm25")
    assert {r["id"] for r in everywhere} == {"named", "described"}
    in_names = adapter.search_by_keywords(query="postgres", limit=10, fields=["name"])
    assert [r["id"] for r in in_names] == ["named"]
    # `content` is not declared on this schema, so nothing can match in it.
    assert adapter.search_by_keywords(query="postgres", fields=["content"]) == []


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"mode": "vector"}, ValueError),
        ({"bm25_k1": 1.2}, ValueError),
        ({"advance": {"post_process_input_limit": 50}}, NotImplementedError),
    ],
)
def test_keyword_search_refuses_what_it_cannot_honour(
    adapter: PgVectorCollectionAdapter, kwargs: dict[str, Any], error: type[Exception]
) -> None:
    """An option with no PostgreSQL form fails rather than being ignored."""
    with pytest.raises(error):
        adapter.search_by_keywords(query="postgres", limit=10, **kwargs)


def test_random_search_runs_through_the_facade(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """The facade always passes ``advance``; ``None`` must be accepted."""
    adapter.upsert([{"id": "a", "level": 1, "vector": vec(1)}])
    results = adapter.search_by_random(
        filter={"op": "must", "field": "level", "conds": [1]}, limit=5
    )
    assert [r["id"] for r in results] == ["a"]
    with pytest.raises(NotImplementedError, match="post_process_ops"):
        adapter.search_by_random(limit=5, advance={"post_process_ops": []})


def test_get_meta_data_raises_when_asked_and_the_registry_row_is_gone(
    dsn: str, adapter: PgVectorCollectionAdapter, test_schema: str
) -> None:
    """``raise_on_error=True`` refuses the in-memory fallback."""
    collection = adapter.get_collection()
    assert collection.get_meta_data(raise_on_error=True)["CollectionName"] == "context"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DELETE FROM {}.{}").format(
                sql.Identifier(test_schema), sql.Identifier("ov_collections")
            )
        )
    assert collection.get_meta_data()["CollectionName"] == "context"
    with pytest.raises(LookupError):
        collection.get_meta_data(raise_on_error=True)


def test_count_with_and_without_filter(adapter: PgVectorCollectionAdapter) -> None:
    """Counting honours the filter it is given."""
    adapter.upsert(
        [
            {"id": "a", "level": 1, "vector": vec(1)},
            {"id": "b", "level": 2, "vector": vec(2)},
            {"id": "c", "level": 2, "vector": vec(3)},
        ]
    )
    assert adapter.count() == 3
    assert adapter.count({"op": "must", "field": "level", "conds": [2]}) == 2


def test_grouped_aggregate(adapter: PgVectorCollectionAdapter) -> None:
    """Grouped counts bucket rows by a scalar column."""
    adapter.upsert(
        [
            {"id": "a", "context_type": "memory", "vector": vec(1)},
            {"id": "b", "context_type": "memory", "vector": vec(2)},
            {"id": "c", "context_type": "skill", "vector": vec(3)},
        ]
    )
    result = adapter.get_collection().aggregate_data(
        index_name="default", op="count", field="context_type"
    )
    assert result.agg == {"memory": 2, "skill": 1}


@pytest.fixture
def path_data(adapter: PgVectorCollectionAdapter) -> list[str]:
    """Populate a small URI hierarchy and return the URIs."""
    uris = [
        "viking://a",
        "viking://a/b",
        "viking://a/b/c",
        "viking://a/b/c/d",
        "viking://other",
    ]
    adapter.upsert([{"id": u, "uri": u, "vector": vec(i)} for i, u in enumerate(uris)])
    return uris


@pytest.mark.parametrize(
    "depth,expected",
    [
        (0, {"viking://a"}),
        (1, {"viking://a", "viking://a/b"}),
        (2, {"viking://a", "viking://a/b", "viking://a/b/c"}),
        (-1, {"viking://a", "viking://a/b", "viking://a/b/c", "viking://a/b/c/d"}),
    ],
)
def test_path_scope_depth(
    adapter: PgVectorCollectionAdapter,
    path_data: list[str],
    depth: int,
    expected: set[str],
) -> None:
    """Depth scoping selects exactly the subtree within budget."""
    results = adapter.query(
        filter={
            "op": "must",
            "field": "uri",
            "conds": ["/a"],
            "para": f"-d={depth}",
        },
        limit=100,
    )
    assert {r["id"] for r in results} == expected


def test_path_scope_root_matches_everything(
    adapter: PgVectorCollectionAdapter, path_data: list[str]
) -> None:
    """Scoping at the root matches every row."""
    results = adapter.query(
        filter={"op": "must", "field": "uri", "conds": ["/"], "para": "-d=-1"},
        limit=100,
    )
    assert len(results) == len(path_data)


def _random_record(rng: random.Random, index: int) -> dict[str, Any]:
    """Build one pseudo-random record spanning every filterable field.

    Some fields are omitted at random so the sweep covers NULL columns, and the
    URI set includes ``_`` and ``%`` so LIKE-metacharacter handling is exercised
    against the reference rather than assumed.
    """
    record: dict[str, Any] = {
        "id": f"r{index}",
        "uri": rng.choice(
            [
                "viking://a",
                "viking://a/b",
                "viking://a/b/c",
                "viking://z",
                "viking://",
                "viking://a_b",
                "viking://aXb/child",
                "viking://a%b",
                "viking://a-b/child",
            ]
        ),
        "context_type": rng.choice(["resource", "memory", "skill"]),
        "name": rng.choice(["alpha", "beta", "gamma", "alpha beta", "50%_x"]),
        "level": rng.choice([0, 1, 2]),
        "active_count": rng.randint(0, 20),
        "search_tags": rng.sample(["x", "y", "z"], k=rng.randint(0, 3)),
        "vector": vec(index),
    }
    # Drop a field now and then so NULL columns are part of the comparison.
    for field in ("name", "level", "search_tags", "context_type"):
        if rng.random() < 0.2:
            del record[field]
    return record


def _candidate_leaves() -> list[dict[str, Any]]:
    """Return the leaf filters the main sweep combines pairwise."""
    leaves: list[dict[str, Any]] = [
        {"op": "must", "field": "context_type", "conds": ["memory"]},
        {"op": "must", "field": "context_type", "conds": ["memory", "skill"]},
        {"op": "must_not", "field": "context_type", "conds": ["memory"]},
        {"op": "must", "field": "level", "conds": [1]},
        {"op": "must_not", "field": "level", "conds": [0, 2]},
        {"op": "range", "field": "active_count", "gte": 5},
        {"op": "range", "field": "active_count", "gt": 3, "lte": 15},
        {"op": "range_out", "field": "active_count", "gte": 5, "lt": 10},
        {"op": "contains", "field": "name", "substring": "alpha"},
        {"op": "contains", "field": "name", "substring": "zzz"},
        {"op": "must", "field": "search_tags", "conds": ["x"]},
        {"op": "must", "field": "search_tags", "conds": ["x", "z"]},
        {"op": "must_not", "field": "search_tags", "conds": ["y"]},
        {"op": "must", "field": "uri", "conds": ["/a"], "para": "-d=0"},
        {"op": "must", "field": "uri", "conds": ["/a"], "para": "-d=1"},
        {"op": "must", "field": "uri", "conds": ["/a"], "para": "-d=-1"},
        {"op": "must_not", "field": "uri", "conds": ["/a"], "para": "-d=1"},
        # LIKE metacharacters in a scoped path must stay literal.
        {"op": "must", "field": "uri", "conds": ["/a_b"], "para": "-d=-1"},
        {"op": "must", "field": "uri", "conds": ["/a%b"], "para": "-d=-1"},
        {"op": "must", "field": "uri", "conds": ["/a-b"], "para": "-d=-1"},
        # A missing value: `None in conds` matches, everything else does not.
        {"op": "must", "field": "name", "conds": [None]},
        {"op": "must_not", "field": "name", "conds": [None]},
        # Undeclared fields evaluate against a missing value.
        {"op": "must", "field": "nope", "conds": ["x"]},
        {"op": "must_not", "field": "nope", "conds": ["x"]},
        {"op": "contains", "field": "nope", "substring": "x"},
        # Empty operand lists.
        {"op": "must", "field": "context_type", "conds": []},
        {"op": "must_not", "field": "context_type", "conds": []},
        # Operands whose type cannot match the column.
        {"op": "must", "field": "level", "conds": ["not-a-number"]},
        {"op": "must", "field": "name", "conds": [7]},
        {"op": "contains", "field": "level", "substring": "1"},
        {"op": "range", "field": "name", "gte": 5},
        # Substring containing LIKE metacharacters.
        {"op": "contains", "field": "name", "substring": "50%_x"},
        # A wrapper around a leaf.
        {"filter": {"op": "must", "field": "context_type", "conds": ["memory"]}},
    ]
    return leaves


# Leaves the native engine does not evaluate (see `LocalReference.evaluates`).
# Listed rather than discovered, so the day the engine starts parsing one of
# them the sweep fails and somebody decides what it should mean here. Until
# then each is pinned, alone and inside an and/or, by
# `test_filters_the_engine_does_not_evaluate_keep_their_answers`.
_UNEVALUATED_LEAVES: list[dict[str, Any]] = [
    {"op": "must", "field": "name", "conds": [None]},
    {"op": "must_not", "field": "name", "conds": [None]},
    {"op": "must", "field": "context_type", "conds": []},
    {"filter": {"op": "must", "field": "context_type", "conds": ["memory"]}},
]

# Leaves the engine evaluates and this backend deliberately answers otherwise.
# OpenViking 0.4's Python evaluator matched `contains` against text only; the
# 0.5 engine stringifies an int64 and substring-matches it. OpenViking builds
# `contains` only for text fields, so this backend keeps the narrower rule
# rather than invent a decimal rendering for numbers.
_DIVERGENT_LEAVES: list[dict[str, Any]] = [
    {"op": "contains", "field": "level", "substring": "1"},
]


def _sweep(
    adapter: PgVectorCollectionAdapter,
    reference: LocalReference,
    leaves: list[dict[str, Any]],
    *,
    unevaluated: list[dict[str, Any]],
    divergent: list[dict[str, Any]],
) -> int:
    """Compare every evaluable leaf, and each pairwise and/or of them.

    Parameters
    ----------
    adapter :
        The backend under test, holding the same records as ``reference``.
    reference :
        OpenViking's local backend.
    leaves :
        Leaf filters to compare and combine.
    unevaluated :
        The leaves the engine is expected not to evaluate. Asserted exactly.
    divergent :
        Leaves left out of the comparison on purpose, each with its reason
        beside the list that names it.

    Returns
    -------
    int
        How many filters were compared.
    """
    skipped = [leaf for leaf in leaves if not reference.evaluates(leaf)]
    assert skipped == unevaluated, (
        "the set of leaves the engine cannot evaluate changed; decide what "
        f"each should mean here, then update the list: {skipped!r}"
    )
    compared = [leaf for leaf in leaves if leaf not in skipped and leaf not in divergent]
    nodes = list(compared)
    for left, right in itertools.combinations(compared, 2):
        nodes.append({"op": "and", "conds": [left, right]})
        nodes.append({"op": "or", "conds": [left, right]})

    mismatches: list[str] = []
    for node in nodes:
        expected = reference.matching_ids(node)
        actual = {r["id"] for r in adapter.query(filter=node, limit=1000)}
        if actual != expected:
            mismatches.append(
                f"filter {node!r}\n  sql-only: {sorted(actual - expected)}"
                f"\n  engine-only: {sorted(expected - actual)}"
            )
    assert not mismatches, f"{len(mismatches)} filters disagree:\n" + "\n".join(
        mismatches
    )
    for leaf in divergent:
        assert reference.evaluates(leaf), f"no longer evaluated by the engine: {leaf!r}"
        assert reference.matching_ids(leaf), f"engine stopped matching: {leaf!r}"
        assert adapter.query(filter=leaf, limit=1000) == [], leaf
    return len(nodes)


def test_filter_semantics_match_reference(adapter: PgVectorCollectionAdapter) -> None:
    """Every filter must select the same rows in SQL as in the native engine.

    OpenViking's local backend is the authoritative statement of what the DSL
    means. Anywhere the SQL disagrees, the SQL is wrong.
    """
    rng = random.Random(20260829)
    records = [_random_record(rng, i) for i in range(60)]
    adapter.upsert(records)
    reference = LocalReference(META)
    reference.upsert(records)

    try:
        checked = _sweep(
            adapter,
            reference,
            _candidate_leaves(),
            unevaluated=_UNEVALUATED_LEAVES,
            divergent=_DIVERGENT_LEAVES,
        )
    finally:
        reference.close()

    assert checked > 100, f"expected a broad sweep, only compared {checked} filters"


@pytest.mark.parametrize(
    "scoped,sibling",
    [
        ("my_notes", "myXnotes"),
        ("my_notes", "my-notes"),
        ("a%b", "aQQb"),
        ("under_score", "underXscore"),
    ],
)
def test_path_scope_does_not_leak_across_siblings(
    adapter: PgVectorCollectionAdapter, scoped: str, sibling: str
) -> None:
    """`_` and `%` in a scoped path must not act as LIKE wildcards.

    The reference uses ``startswith``; an unescaped LIKE would pull in sibling
    subtrees, which breaks the containment that path scope exists to provide.
    """
    adapter.upsert(
        [
            {
                "id": "in",
                "uri": f"viking://user/default/{scoped}/private",
                "vector": vec(1),
            },
            {
                "id": "out",
                "uri": f"viking://user/default/{sibling}/other",
                "vector": vec(2),
            },
        ]
    )
    node = {
        "op": "must",
        "field": "uri",
        "conds": [f"/user/default/{scoped}"],
        "para": "-d=-1",
    }
    assert {r["id"] for r in adapter.query(filter=node, limit=50)} == {"in"}

    negated = dict(node, op="must_not")
    assert {r["id"] for r in adapter.query(filter=negated, limit=50)} == {"out"}


def test_path_scope_ignores_surrounding_whitespace(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Whitespace is stripped as Python's ``str.strip()`` does, not just spaces."""
    adapter.upsert({"id": "a", "uri": "viking://user/default/notes", "vector": vec(1)})
    node = {
        "op": "must",
        "field": "uri",
        "conds": ["\t/user/default/notes\n"],
        "para": "-d=-1",
    }
    assert {r["id"] for r in adapter.query(filter=node, limit=10)} == {"a"}


@pytest.mark.parametrize(
    "node",
    [
        {"op": "contains", "field": "level", "substring": "1"},
        {"op": "range", "field": "name", "gte": 5},
        {"op": "range", "field": "search_tags", "gte": 1},
        {"op": "must", "field": "name", "conds": ["alpha", 1]},
        {"op": "must", "field": "name", "conds": [1]},
        {"op": "must", "field": "uri", "conds": [123]},
        {"op": "must", "field": "level", "conds": ["not-a-number"]},
    ],
)
def test_type_mismatched_operands_match_nothing(
    adapter: PgVectorCollectionAdapter, node: dict[str, Any]
) -> None:
    """A mismatched operand selects no rows instead of raising.

    The reference compares with ``in``/``==``/``<`` and catches ``TypeError``,
    so it yields an empty result. Binding the operand raw would instead make
    PostgreSQL raise ``UndefinedFunction`` and fail the whole query.
    """
    adapter.upsert(
        [
            {
                "id": "a",
                "name": "alpha",
                "level": 1,
                "uri": "viking://a",
                "vector": vec(1),
            },
            {"id": "b", "name": "beta", "level": 2, "vector": vec(2)},
        ]
    )
    results = adapter.query(filter=node, limit=50)
    assert {r["id"] for r in results} <= {"a"}


def test_output_fields_excludes_out_of_schema_data(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """An explicit projection must not smuggle `extra` back in."""
    adapter.upsert(
        {"id": "a", "name": "n", "vector": vec(1), "secret": "LEAK", "bulk": [1] * 50}
    )
    record = adapter.query(query_vector=vec(1), output_fields=["name"], limit=1)[0]
    assert record["name"] == "n"
    assert "secret" not in record
    assert "bulk" not in record


def test_extra_is_returned_when_no_projection_given(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Without a projection, out-of-schema fields still round-trip."""
    adapter.upsert({"id": "a", "custom": {"k": 1}, "vector": vec(1)})
    assert adapter.get(["a"])[0]["custom"] == {"k": 1}


def test_upsert_replaces_the_whole_row(adapter: PgVectorCollectionAdapter) -> None:
    """Upsert replaces, matching LocalCollection's whole-document write."""
    adapter.upsert({"id": "z", "name": "A", "level": 1, "vector": vec(1)})
    adapter.upsert({"id": "z", "name": "B", "vector": vec(1)})
    record = adapter.get(["z"])[0]
    assert record["name"] == "B"
    # Replaced, so `level` reverts to the engine default rather than keeping 1.
    assert record["level"] == 0


def test_upsert_batch_applies_in_input_order(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """A repeated primary key in one batch ends on its last occurrence."""
    adapter.upsert(
        [
            {"id": "w", "name": "N1", "vector": vec(1)},
            {"id": "w", "name": "N2", "level": 5, "vector": vec(1)},
            {"id": "w", "name": "N3", "vector": vec(1)},
        ]
    )
    record = adapter.get(["w"])[0]
    assert record["name"] == "N3"
    assert record["level"] == 0


def test_update_data_rejects_unknown_primary_key(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Updating a row that does not exist is an error, as on the local backend."""
    with pytest.raises(ValueError, match="not found"):
        adapter.get_collection().update_data([{"id": "ghost", "name": "x"}])


def test_conds_containing_none_matches_a_null_column(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """``None in conds`` matches a row whose column is genuinely unset.

    Once engine defaults are applied, a scalar is never missing -- a string is
    ``""``, an int is ``0``. Only ``date_time`` and ``geo_point`` stay NULL,
    because the engine drops them when empty. The engine cannot be used as an
    oracle here: it does not evaluate a ``None`` operand (see
    ``tests/reference.py``), so this pins our own behaviour instead.
    """
    adapter.upsert(
        [
            {"id": "dated", "created_at": "2026-01-01T00:00:00Z", "vector": vec(1)},
            {"id": "undated", "vector": vec(2)},
        ]
    )
    node = {"op": "must", "field": "created_at", "conds": [None]}
    assert {r["id"] for r in adapter.query(filter=node, limit=10)} == {"undated"}


def test_range_out_on_unknown_field_matches_everything(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """A negated test against an undeclared field excludes nothing."""
    adapter.upsert([{"id": f"r{i}", "vector": vec(i)} for i in range(3)])
    node = {"op": "range_out", "field": "nope", "gte": 1}
    assert len(adapter.query(filter=node, limit=10)) == 3


def test_read_modify_write_preserves_vectors(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """A fetched record fed back into upsert must keep its embedding.

    ``upsert_data`` replaces the whole row, and OpenViking's
    ``increment_active_count`` and ``update_uri_mapping`` both do
    ``upsert(record | {...})`` on a record from ``get()``. If ``get()`` omitted
    the vector, the upsert would write NULL over it and the row would vanish
    from every vector search while still existing in the table.
    """
    adapter.upsert(
        {
            "id": "a",
            "name": "doc",
            "active_count": 0,
            "sparse_vector": {"7": 1.0},
            "vector": vec(1),
        }
    )
    assert [r["id"] for r in adapter.query(query_vector=vec(1), limit=5)] == ["a"]

    record = adapter.get(["a"])[0]
    assert "vector" in record, "get() must return vectors for read-modify-write"
    assert "sparse_vector" in record

    adapter.upsert(record | {"active_count": 1})

    assert [r["id"] for r in adapter.query(query_vector=vec(1), limit=5)] == ["a"]
    after = adapter.get(["a"])[0]
    assert after["active_count"] == 1
    assert after["vector"] == record["vector"]
    assert after["sparse_vector"] == {"7": 1.0}


def test_search_projection_still_excludes_vectors(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Search results stay lean; only ``get()`` returns the full row.

    Guards the fix above from being widened into ``_output_columns``, which
    would put a 512-float vector in every search result.
    """
    adapter.upsert({"id": "a", "name": "doc", "vector": vec(1)})
    hit = adapter.query(query_vector=vec(1), limit=1)[0]
    assert "vector" not in hit
    assert "sparse_vector" not in hit


WIDE_META: dict[str, Any] = {
    "CollectionName": "context",
    "Description": "collection covering the field types the context schema omits",
    "Fields": [
        {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
        {"FieldName": "flag", "FieldType": "bool"},
        {"FieldName": "score", "FieldType": "float32"},
        {"FieldName": "counts", "FieldType": "list<int64>"},
        {"FieldName": "body", "FieldType": "text"},
        {"FieldName": "vector", "FieldType": "vector", "Dim": DIM},
    ],
    "ScalarIndex": ["flag", "score", "counts", "body"],
}


@pytest.fixture
def wide_adapter(dsn: str, test_schema: str) -> Iterator[PgVectorCollectionAdapter]:
    """Yield an adapter over a collection declaring the less common types."""
    inst = make_adapter(dsn, test_schema)
    inst.create_collection(
        "context", WIDE_META, distance="cosine", sparse_weight=0.0, index_name="default"
    )
    try:
        yield inst
    finally:
        inst.close()


def _wide_record(rng: random.Random, index: int) -> dict[str, Any]:
    """Build one pseudo-random record over bool, float32, list<int64> and text."""
    record: dict[str, Any] = {
        "id": f"w{index}",
        "flag": rng.choice([True, False]),
        "score": rng.choice([0.1, 0.5, 1.0, 2.5, 9.75]),
        "counts": rng.sample([1, 2, 3, 5, 8], k=rng.randint(0, 3)),
        "body": rng.choice(["alpha text", "beta text", "gamma", "50%_x"]),
        "vector": vec(index),
    }
    for field in ("flag", "score", "counts", "body"):
        if rng.random() < 0.2:
            del record[field]
    return record


def _wide_candidate_leaves() -> list[dict[str, Any]]:
    """Return the leaf filters over the wide schema that its sweep combines."""
    leaves: list[dict[str, Any]] = [
        {"op": "must", "field": "flag", "conds": [True]},
        {"op": "must", "field": "flag", "conds": [False]},
        {"op": "must_not", "field": "flag", "conds": [True]},
        {"op": "must", "field": "score", "conds": [0.1]},
        {"op": "must", "field": "score", "conds": [0.1, 9.75]},
        {"op": "must_not", "field": "score", "conds": [0.5]},
        {"op": "range", "field": "score", "gte": 0.5},
        {"op": "range", "field": "score", "gt": 0.1, "lte": 2.5},
        {"op": "range_out", "field": "score", "gte": 0.5, "lt": 2.5},
        {"op": "must", "field": "counts", "conds": [3]},
        {"op": "must", "field": "counts", "conds": [1, 8]},
        {"op": "must_not", "field": "counts", "conds": [2]},
        {"op": "must", "field": "body", "conds": ["gamma"]},
        {"op": "must_not", "field": "body", "conds": ["gamma"]},
        {"op": "contains", "field": "body", "substring": "text"},
        {"op": "contains", "field": "body", "substring": "50%_x"},
        {"op": "range", "field": "counts", "gte": 2},
        {"op": "must", "field": "flag", "conds": [None]},
        {"op": "must", "field": "score", "conds": [None]},
        {"op": "must", "field": "flag", "conds": ["not-a-bool"]},
        {"op": "must", "field": "score", "conds": ["not-a-number"]},
        {"op": "must", "field": "counts", "conds": ["not-an-int"]},
        {"op": "contains", "field": "score", "substring": "0"},
        {"op": "must", "field": "body", "conds": [7]},
    ]
    return leaves


# As `_UNEVALUATED_LEAVES`, for the wide schema: the engine evaluates no
# equality test on a float32 field, and no `None` operand at all. The same
# shapes are pinned by `test_filters_the_engine_does_not_evaluate_keep_their_answers`.
_WIDE_UNEVALUATED_LEAVES: list[dict[str, Any]] = [
    {"op": "must", "field": "score", "conds": [0.1]},
    {"op": "must", "field": "score", "conds": [0.1, 9.75]},
    {"op": "must_not", "field": "score", "conds": [0.5]},
    {"op": "must", "field": "flag", "conds": [None]},
    {"op": "must", "field": "score", "conds": [None]},
]


def test_filter_semantics_match_reference_for_remaining_types(
    wide_adapter: PgVectorCollectionAdapter,
) -> None:
    """Extend the differential comparison to bool, float32, list<int64> and text.

    The main sweep runs against the ``context`` schema, which declares only
    ``string``, ``path``, ``int64`` and ``list<string>``. The casts this
    backend applies for the other types -- ``::real[]`` for float32,
    ``::bigint[]`` for list<int64> -- and the deliberate bool/int64 exclusion in
    ``_is_comparable`` were previously asserted only against hand-written
    expectations. This checks them against the reference evaluator instead.
    """
    rng = random.Random(20260830)
    records = [_wide_record(rng, i) for i in range(60)]
    wide_adapter.upsert(records)
    reference = LocalReference(WIDE_META)
    reference.upsert(records)

    try:
        checked = _sweep(
            wide_adapter,
            reference,
            _wide_candidate_leaves(),
            unevaluated=_WIDE_UNEVALUATED_LEAVES,
            divergent=[],
        )
    finally:
        reference.close()

    assert checked > 300, f"expected a broad sweep, only compared {checked} filters"


@pytest.mark.parametrize(
    "op,field,expect",
    [
        ("range", "search_tags", {"has", "empty"}),
        ("range", "level", {"has", "empty"}),
        ("range_out", "search_tags", set()),
        ("range_out", "level", set()),
    ],
)
def test_range_without_bounds_matches_present_values(
    adapter: PgVectorCollectionAdapter, op: str, field: str, expect: set[str]
) -> None:
    """A range carrying no bounds tests only for a present value.

    ``Range(field)`` with every bound left as None compiles to a bare
    ``{"op": "range", "field": ...}``. OpenViking 0.4's evaluator rejected only
    a missing *value*, so a present one passed every absent check and matched;
    guarding array columns unconditionally made this return nothing. The 0.5
    engine does not evaluate a range without bounds, so the expectation is
    pinned to the 0.4 answer.
    """
    records: list[dict[str, Any]] = [
        {"id": "has", "level": 3, "search_tags": ["x"], "vector": vec(1)},
        {"id": "empty", "vector": vec(2)},
    ]
    adapter.upsert(records)

    node = {"op": op, "field": field}
    assert {r["id"] for r in adapter.query(filter=node, limit=50)} == expect
    assert engine_answer(META, records, node) in (None, expect)


# The answer OpenViking's 0.5 engine gives where it differs from 0.4's. It
# has no range over a bool and no bool operand against a float32, and answers
# both with nothing. OpenViking never builds either filter, so this backend
# keeps 0.4's `True == 1`; these entries make a change on either side visible.
_ENGINE_ANSWERS_NOTHING: set[str] = set()


@pytest.mark.parametrize(
    "node,expect,engine",
    [
        ({"op": "must", "field": "flag", "conds": [1]}, {"t"}, None),
        ({"op": "must", "field": "flag", "conds": [0]}, {"f", "none"}, None),
        ({"op": "must_not", "field": "flag", "conds": [1]}, {"f", "none"}, None),
        (
            {"op": "range", "field": "flag", "gte": 0},
            {"t", "f", "none"},
            _ENGINE_ANSWERS_NOTHING,
        ),
        ({"op": "must", "field": "flag", "conds": [2]}, set(), None),
        (
            {"op": "must", "field": "score", "conds": [True]},
            {"t"},
            _ENGINE_ANSWERS_NOTHING,
        ),
        ({"op": "range", "field": "score", "gte": True}, {"t"}, None),
    ],
)
def test_bool_and_int_operands_interoperate(
    wide_adapter: PgVectorCollectionAdapter,
    node: dict[str, Any],
    expect: set[str],
    engine: set[str] | None,
) -> None:
    """Python compares ``True == 1``; the SQL must agree in both directions.

    PostgreSQL has no implicit boolean/bigint comparison, so these operands are
    converted rather than dropped. ``2`` against a boolean column still matches
    nothing, because ``True == 2`` is false as well. ``none`` takes the
    engine's default of ``False``. Where the engine evaluates the node it must
    agree, unless ``engine`` names its differing answer; where it does not
    evaluate the node, the answer is OpenViking 0.4's.
    """
    records: list[dict[str, Any]] = [
        {"id": "t", "flag": True, "score": 1.0, "vector": vec(1)},
        {"id": "f", "flag": False, "score": 0.0, "vector": vec(2)},
        {"id": "none", "vector": vec(3)},
    ]
    wide_adapter.upsert(records)

    assert {r["id"] for r in wide_adapter.query(filter=node, limit=50)} == expect
    if engine is None:
        assert engine_answer(WIDE_META, records, node) in (None, expect)
    else:
        assert engine_answer(WIDE_META, records, node) == engine


def test_a_record_without_an_embedding_is_refused(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Writing a record with no vector must fail, as it does on the engine.

    ``DataProcessor`` marks the vector field Required, so such a row cannot
    exist there. Storing one here would also make it unreachable: pgvector's
    HNSW and IVFFlat builds skip NULL vectors, so an index scan can never
    return it -- it would be counted but never found or deleted.
    """
    for record in ({"id": "novec", "level": 1}, {"id": "nullvec", "vector": None}):
        with pytest.raises(ValueError, match="embedding is required"):
            adapter.upsert(record)
    assert adapter.count() == 0


def test_omitted_fields_take_the_engine_defaults(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """An omitted scalar must hold the engine's default, not NULL.

    ``DataProcessor`` builds a per-type default into the validator every write
    passes through, so on the built-in backend an absent ``level`` is ``0``.
    Storing NULL here made ``level == 0`` match there and miss here.
    """
    record = {"id": "omit", "vector": vec(1)}
    adapter.upsert(record)

    for node in (
        {"op": "must", "field": "level", "conds": [0]},
        {"op": "range", "field": "level", "lte": 1},
        {"op": "must", "field": "name", "conds": [""]},
        {"op": "must", "field": "search_tags", "conds": []},
    ):
        actual = {r["id"] for r in adapter.query(filter=node, limit=10)}
        # An empty operand list matches nothing; the others match the default.
        expect = set() if node.get("conds") == [] else {"omit"}
        assert actual == expect, node
        assert engine_answer(META, record, node) in (None, expect), node

    stored = adapter.get(["omit"])[0]
    assert stored["level"] == 0
    assert stored["name"] == ""
    assert stored["search_tags"] == []


def test_mixed_comparable_and_incomparable_bounds(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """A bound short-circuit must not leave its predecessor's parameter behind.

    ``Range(level, gte=1, lte="5")`` binds the integer bound, then hits the
    string bound and returns early. The orphaned parameter desynchronised the
    whole tree and PostgreSQL rejected the statement.
    """
    adapter.upsert({"id": "a", "level": 3, "vector": vec(1)})
    node = {"op": "range", "field": "level", "gte": 1, "lte": "5"}
    assert adapter.query(filter=node, limit=10) == []

    # The parameter list must still line up when the bad bound sits inside a
    # larger tree alongside other bound operands.
    combined = {
        "op": "and",
        "conds": [node, {"op": "must", "field": "id", "conds": ["a"]}],
    }
    assert adapter.query(filter=combined, limit=10) == []


@pytest.mark.parametrize(
    "node",
    [
        {"op": "must", "field": "created_at", "conds": [True]},
        {"op": "must_not", "field": "created_at", "conds": [True]},
        {"op": "range", "field": "created_at", "gte": True},
    ],
)
def test_bool_operand_on_datetime_does_not_raise(
    adapter: PgVectorCollectionAdapter, node: dict[str, Any]
) -> None:
    """A bool against a timestamp column matches nothing rather than raising.

    ``parse_datetime_to_epoch_ms`` rejects bool outright, so admitting one as a
    comparable operand turned a correct empty result into a crash.
    """
    adapter.upsert({"id": "a", "created_at": "2026-01-01T00:00:00Z", "vector": vec(1)})
    adapter.query(filter=node, limit=10)


def test_sparse_only_rows_stay_ordered_among_themselves(
    dsn: str, test_schema: str
) -> None:
    """Sparse contribution must order rows the dense term cannot separate.

    Both rows sit at the same dense distance, so any difference in ranking is
    attributable to the sparse term. A sentinel folded into the score used to
    collapse these to one value.
    """
    config = VectorDBBackendConfig(
        backend="ov_postgres.adapter.PgVectorCollectionAdapter",
        name="context",
        index_name="default",
        distance_metric="cosine",
        sparse_weight=1.0,
        custom_params={"dsn": dsn, "schema": test_schema},
    )
    adapter = PgVectorCollectionAdapter.from_config(config)
    adapter.create_collection(
        "context", META, distance="cosine", sparse_weight=1.0, index_name="default"
    )
    try:
        shared = [1.0] + [0.0] * (DIM - 1)
        adapter.upsert(
            [
                {"id": "strong", "vector": shared, "sparse_vector": {"7": 100.0}},
                {"id": "weak", "vector": shared, "sparse_vector": {"7": 1.0}},
            ]
        )
        results = adapter.query(
            query_vector=[1.0] + [0.0] * (DIM - 1),
            sparse_query_vector={"7": 1.0},
            limit=10,
        )
        assert [r["id"] for r in results] == ["strong", "weak"]
        assert results[0]["_score"] > results[1]["_score"]
    finally:
        adapter.close()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", float("nan"), float("inf")])
def test_non_finite_numbers_are_rejected_on_write(
    adapter: PgVectorCollectionAdapter, bad: object
) -> None:
    """NaN and infinity are refused, including as strings.

    PostgreSQL accepts ``'NaN'`` for a numeric column and then sorts it above
    every number, while the reference never matches it -- so every range filter
    on that row would diverge.
    """
    with pytest.raises(ValueError, match="NaN and infinity"):
        adapter.upsert({"id": "bad", "active_count": bad, "vector": vec(1)})


def test_backfill_defaults_repairs_older_rows(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Rows written before defaults existed are brought into line.

    Defaults apply on write, so an upgraded database holds two populations
    until this runs: a filter for ``level == 0`` would find the new rows and
    miss the old ones.
    """
    adapter.upsert({"id": "new", "vector": vec(1)})

    # Simulate a row from before defaults were applied.
    collection = adapter.get_collection()
    inner = collection._Collection__collection
    inner._execute(
        sql.SQL("INSERT INTO {}.{} (id, vector) VALUES (%s, %s::vector)").format(
            sql.Identifier(inner._db_schema), sql.Identifier(inner._table)
        ),
        ("old", "[" + ",".join(["0.1"] * DIM) + "]"),
    )
    node = {"op": "must", "field": "level", "conds": [0]}
    assert {r["id"] for r in adapter.query(filter=node, limit=10)} == {"new"}

    assert inner.backfill_defaults() == 1
    assert {r["id"] for r in adapter.query(filter=node, limit=10)} == {"new", "old"}

    # Idempotent: a second run has nothing left to repair.
    assert inner.backfill_defaults() == 0


def test_legacy_rows_without_embeddings_rank_last_and_score_low(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """A NULL-vector row from before embeddings were required is handled.

    New writes are refused, but a database written by an older version may
    hold such rows. They rank behind every embedded row and report a score
    below any real one, because OpenViking's retriever re-sorts by ``_score``
    and applies an absolute threshold.
    """
    adapter.upsert({"id": "real", "vector": [1.0] + [0.0] * (DIM - 1)})

    collection = adapter.get_collection()
    inner = collection._Collection__collection
    inner._execute(
        sql.SQL("INSERT INTO {}.{} (id) VALUES (%s)").format(
            sql.Identifier(inner._db_schema), sql.Identifier(inner._table)
        ),
        ("legacy",),
    )

    results = adapter.query(query_vector=[1.0] + [0.0] * (DIM - 1), limit=10)
    assert [r["id"] for r in results] == ["real", "legacy"]
    assert results[-1]["_score"] < results[0]["_score"]
    assert results[-1]["_score"] <= NO_VECTOR_SCORE


# Each leaf the engine does not evaluate, with OpenViking 0.4's answer over
# the records below: `named` has a name, `blank` takes the default "" (a
# present value, so `None` never matches it), and `flag`/`score` default to
# False/0.0 on the wide schema.
_PINNED_UNEVALUATED: list[tuple[bool, dict[str, Any], set[str]]] = [
    (False, {"op": "must", "field": "name", "conds": [None]}, set()),
    (False, {"op": "must_not", "field": "name", "conds": [None]}, {"named", "blank"}),
    (False, {"op": "must", "field": "context_type", "conds": []}, set()),
    (
        False,
        {"filter": {"op": "must", "field": "context_type", "conds": ["memory"]}},
        {"named"},
    ),
    (True, {"op": "must", "field": "score", "conds": [0.5]}, {"named"}),
    (True, {"op": "must", "field": "score", "conds": [0.5, 9.75]}, {"named"}),
    (True, {"op": "must_not", "field": "score", "conds": [0.5]}, {"blank"}),
    (True, {"op": "must", "field": "flag", "conds": [None]}, set()),
    (True, {"op": "must", "field": "score", "conds": [None]}, set()),
]


@pytest.mark.parametrize(
    "wide,leaf,expect",
    _PINNED_UNEVALUATED,
    ids=[repr(leaf) for _, leaf, _ in _PINNED_UNEVALUATED],
)
def test_filters_the_engine_does_not_evaluate_keep_their_answers(
    dsn: str, test_schema: str, wide: bool, leaf: dict[str, Any], expect: set[str]
) -> None:
    """Pin the 0.4 answer where the 0.5 engine has none, alone and combined.

    The engine drops these filters and returns every row, so the sweep cannot
    use it. ANDed with a condition both records meet the answer must not
    change, and ORed with one neither meets it must not change either.
    """
    meta = WIDE_META if wide else META
    records: list[dict[str, Any]] = (
        [
            {"id": "named", "flag": True, "score": 0.5, "vector": vec(1)},
            {"id": "blank", "vector": vec(2)},
        ]
        if wide
        else [
            {"id": "named", "name": "n", "context_type": "memory", "vector": vec(1)},
            {"id": "blank", "context_type": "resource", "vector": vec(2)},
        ]
    )
    inst = make_adapter(dsn, test_schema)
    inst.create_collection(
        "context", meta, distance="cosine", sparse_weight=0.0, index_name="default"
    )
    try:
        inst.upsert(records)
        everyone = {"op": "must", "field": "id", "conds": ["named", "blank"]}
        nobody = {"op": "must", "field": "id", "conds": ["ghost"]}
        for node in (
            leaf,
            {"op": "and", "conds": [leaf, everyone]},
            {"op": "or", "conds": [nobody, leaf]},
        ):
            assert {r["id"] for r in inst.query(filter=node, limit=10)} == expect, node
    finally:
        inst.close()
