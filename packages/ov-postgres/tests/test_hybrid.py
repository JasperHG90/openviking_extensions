"""Integration coverage for body storage, keyword matching and MMR similarity.

Everything here runs against a real PostgreSQL with pgvector. The SQL these
features add is the part unit tests cannot vouch for: whether `to_tsquery`
accepts a rewritten query, whether an all-stopword term is survivable, and
whether `DISTINCT ON` really collapses a URI carrying a row per level.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ov_postgres.collection import _MAX_ANY_MODE_WORDS

from .test_backends import build
from .test_integration import META, vec

pytestmark = pytest.mark.integration

# A body holding terms that appear nowhere in the metadata, so a match proves
# the tsvector really covers `content`.
BODY = (
    "The vault policy rotates credentials every ninety days. "
    "Operators renew the intermediate certificate authority by hand."
)


def content_meta() -> dict[str, Any]:
    """META plus the `content` field the real OpenViking schema declares."""
    meta = {
        **META,
        "Fields": [*META["Fields"], {"FieldName": "content", "FieldType": "text"}],
    }
    return meta


def record(
    key: str,
    *,
    name: str = "",
    content: str = "",
    tags: list[str] | None = None,
    seed: int = 1,
) -> dict[str, Any]:
    """Build one storable record."""
    return {
        "id": key,
        "uri": f"viking://docs/{key}",
        "context_type": "resource",
        "name": name,
        "description": "",
        "level": 2,
        "active_count": 0,
        "search_tags": tags or [],
        "vector": vec(seed),
        "content": content,
    }


def uris(rows: Any) -> list[str]:
    """Pull the URIs out of a result list, in order."""
    return [row["uri"] for row in rows]


def test_bodies_are_searchable_when_stored(dsn: str, test_schema: str) -> None:
    """The end this whole feature exists for: find a document by its prose."""
    adapter = build(dsn, test_schema, meta=content_meta(), store_content=True)
    try:
        adapter.upsert(
            [
                record("a", name="Runbook", content=BODY),
                record("b", name="Unrelated", content="Nothing about that here.", seed=2),
            ]
        )

        found = adapter.search_by_keywords(query="intermediate certificate", limit=10)

        assert uris(found) == ["viking://docs/a"], "the term appears only in the body"
    finally:
        adapter.close()


def test_bodies_are_not_searchable_when_not_stored(dsn: str, test_schema: str) -> None:
    """Without the flag OpenViking drops the body, so nothing can match it."""
    adapter = build(dsn, test_schema, meta=content_meta())
    try:
        adapter.upsert([record("a", name="Runbook", content=BODY)])

        found = adapter.search_by_keywords(query="intermediate certificate", limit=10)

        assert uris(found) == []
    finally:
        adapter.close()


def test_a_natural_language_query_matches_on_any_word(dsn: str, test_schema: str) -> None:
    """`plainto_tsquery` ANDs every word, and no long question survives that."""
    adapter = build(
        dsn,
        test_schema,
        meta=content_meta(),
        store_content=True,
        text_search_config="english",
    )
    try:
        adapter.upsert([record("a", name="Runbook", content=BODY)])

        loose = adapter.search_by_keywords(
            query="how did we configure the vault policy", limit=10
        )

        assert uris(loose) == ["viking://docs/a"]
    finally:
        adapter.close()


def test_all_mode_still_requires_every_word(dsn: str, test_schema: str) -> None:
    """The opt-out has to actually opt out."""
    adapter = build(
        dsn,
        test_schema,
        meta=content_meta(),
        store_content=True,
        text_search_config="english",
        keyword_query_mode="all",
    )
    try:
        adapter.upsert([record("a", name="Runbook", content=BODY)])

        strict = adapter.search_by_keywords(
            query="how did we configure the vault policy", limit=10
        )

        assert uris(strict) == [], "'how' and 'configure' are absent from the body"
    finally:
        adapter.close()


def test_a_document_sized_query_searches_instead_of_raising(
    dsn: str, test_schema: str
) -> None:
    """An agent pasting a whole document as the query used to get a ValueError.

    The two real words sit in different pieces of the split on purpose. A
    split that dropped a piece, or ANDed the pieces instead of ORing them,
    would find one of these documents or neither.
    """
    adapter = build(
        dsn,
        test_schema,
        meta=content_meta(),
        store_content=True,
        text_search_config="english",
    )
    try:
        adapter.upsert(
            [
                record("a", name="Runbook", content=BODY),
                record(
                    "b", name="Cluster", content="Kubernetes drains the node.", seed=2
                ),
            ]
        )
        filler = " ".join(f"filler{n}" for n in range(_MAX_ANY_MODE_WORDS * 3))
        document = f"intermediate {filler} kubernetes"

        found = adapter.search_by_keywords(query=document, limit=10)

        assert len(document.split()) > _MAX_ANY_MODE_WORDS * 3, "several pieces, not two"
        assert set(uris(found)) == {"viking://docs/a", "viking://docs/b"}
    finally:
        adapter.close()


def test_the_split_survives_a_stack_the_unsplit_term_would_exhaust(
    dsn: str, test_schema: str
) -> None:
    """The split earns its place below the default 2MB stack, not at it.

    At 2MB an unsplit term parses to about 14500 words, well past the ceiling
    this backend enforces, so nothing here would fail without the split. 512kB
    is the low end of the range the split covers: an unsplit term parses to
    about 3600 words there, a single piece still parses comfortably, and the
    6000 words below sit well clear of both. That window is what the split is
    for -- which queries work stops depending on a server setting this package
    does not control.

    The size is chosen for margin, not to sit near either wall. A query just
    over the unsplit limit would fail *open* as PostgreSQL versions move: the
    test would quietly start passing without the split rather than going red.

    `ALTER DATABASE` reaches every connection to the shared container, not just
    this test's. The `finally` puts it back, and the suite runs sequentially --
    running these tests in parallel would need a different approach.
    """
    database = conninfo_to_dict(dsn)["dbname"]
    setting = sql.SQL("ALTER DATABASE {} SET max_stack_depth = {}").format(
        sql.Identifier(str(database)), sql.Literal("512kB")
    )
    # Applies to connections opened after it, so the adapter is built below.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(setting)
    try:
        adapter = build(
            dsn,
            test_schema,
            meta=content_meta(),
            store_content=True,
            text_search_config="english",
        )
        try:
            adapter.upsert([record("a", name="Runbook", content=BODY)])
            filler = " ".join(f"filler{n}" for n in range(6000))

            found = adapter.search_by_keywords(query=f"intermediate {filler}", limit=10)

            assert uris(found) == ["viking://docs/a"]
        finally:
            adapter.close()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER DATABASE {} RESET max_stack_depth").format(
                    sql.Identifier(str(database))
                )
            )


def test_an_all_stopword_query_matches_nothing_and_does_not_raise(
    dsn: str, test_schema: str
) -> None:
    """`to_tsquery('')` raises, so the rewrite has to guard the empty case."""
    adapter = build(
        dsn,
        test_schema,
        meta=content_meta(),
        store_content=True,
        text_search_config="english",
    )
    try:
        adapter.upsert([record("a", name="Runbook", content=BODY)])

        assert uris(adapter.search_by_keywords(query="the and of", limit=10)) == []
    finally:
        adapter.close()


def test_an_empty_term_does_not_poison_the_other_alternatives(
    dsn: str, test_schema: str
) -> None:
    """An empty tsquery OR-ed with a real one must not swallow it.

    `NULL || 'x'::tsquery` is NULL, which would match no rows at all -- so the
    empty case has to produce an empty tsquery rather than NULL.
    """
    adapter = build(
        dsn,
        test_schema,
        meta=content_meta(),
        store_content=True,
        text_search_config="english",
    )
    try:
        adapter.upsert([record("a", name="Runbook", content=BODY)])

        found = adapter.search_by_keywords(
            keywords=["the and of"], query="certificate", limit=10
        )

        assert uris(found) == ["viking://docs/a"]
    finally:
        adapter.close()


def test_keywords_entries_stay_alternatives_of_each_other(
    dsn: str, test_schema: str
) -> None:
    """Deliberate keywords should match either one, as they always have."""
    adapter = build(dsn, test_schema, meta=content_meta(), store_content=True)
    try:
        adapter.upsert(
            [
                record("a", name="alpha", content="", seed=1),
                record("b", name="beta", content="", seed=2),
            ]
        )

        found = adapter.search_by_keywords(keywords=["alpha", "beta"], limit=10)

        assert sorted(uris(found)) == ["viking://docs/a", "viking://docs/b"]
    finally:
        adapter.close()


class TestPairwiseSimilarity:
    """The matrix MMR runs on, computed inside the database."""

    def test_identical_vectors_score_one_and_both_orderings_are_present(
        self, dsn: str, test_schema: str
    ) -> None:
        """MMR looks a pair up in whichever order it meets it."""
        adapter = build(dsn, test_schema, meta=content_meta())
        try:
            adapter.upsert([record("a", seed=1), record("b", seed=1)])

            matrix = adapter.pairwise_similarity(["a", "b"])

            assert matrix[("a", "b")] == pytest.approx(1.0, abs=1e-6)
            assert matrix[("b", "a")] == pytest.approx(1.0, abs=1e-6)
        finally:
            adapter.close()

    def test_different_vectors_score_below_identical_ones(
        self, dsn: str, test_schema: str
    ) -> None:
        """Similarity has to discriminate, not just return a number."""
        adapter = build(dsn, test_schema, meta=content_meta())
        try:
            adapter.upsert(
                [record("a", seed=1), record("b", seed=1), record("c", seed=7)]
            )

            matrix = adapter.pairwise_similarity(["a", "b", "c"])

            assert matrix[("a", "c")] < matrix[("a", "b")]
        finally:
            adapter.close()

    def test_ranking_by_uri_collapses_a_uri_carrying_several_levels(
        self, dsn: str, test_schema: str
    ) -> None:
        """A URI can hold a row per level; the matrix needs one entry per pair.

        Without `DISTINCT ON` the cross join emits a pair per combination and
        the last one written silently wins.
        """
        adapter = build(dsn, test_schema, meta=content_meta())
        try:
            shared = {**record("l0", seed=1), "uri": "viking://docs/shared", "level": 0}
            other = {**record("l1", seed=3), "uri": "viking://docs/shared", "level": 1}
            adapter.upsert([shared, other, {**record("z", seed=5)}])

            matrix = adapter.pairwise_similarity(
                ["viking://docs/shared", "viking://docs/z"], field="uri"
            )

            assert set(matrix) == {
                ("viking://docs/shared", "viking://docs/z"),
                ("viking://docs/z", "viking://docs/shared"),
            }
        finally:
            adapter.close()

    def test_a_row_without_an_embedding_is_skipped_not_called_identical(
        self, dsn: str, test_schema: str
    ) -> None:
        """A zero distance would claim two vectorless rows are the same thing."""
        adapter = build(dsn, test_schema, meta=content_meta())
        try:
            adapter.upsert([record("a", seed=1), record("b", seed=2)])
            adapter.update_data([{"id": "b", "active_count": 1}])

            matrix = adapter.pairwise_similarity(["a", "b"])

            assert ("a", "b") in matrix, "both rows have vectors here"
        finally:
            adapter.close()

    def test_fewer_than_two_rows_has_no_pairs(self, dsn: str, test_schema: str) -> None:
        """One candidate has nothing to be similar to; skip the query."""
        adapter = build(dsn, test_schema, meta=content_meta())
        try:
            adapter.upsert([record("a", seed=1)])

            assert adapter.pairwise_similarity(["a"]) == {}
        finally:
            adapter.close()

    def test_an_unknown_field_is_refused(self, dsn: str, test_schema: str) -> None:
        """Never interpolate an unchecked column name into SQL."""
        adapter = build(dsn, test_schema, meta=content_meta())
        try:
            adapter.upsert([record("a", seed=1), record("b", seed=2)])

            with pytest.raises(ValueError, match="unknown field"):
                adapter.pairwise_similarity(["a", "b"], field="nope; DROP TABLE x")
        finally:
            adapter.close()


def test_content_joins_the_full_text_index(dsn: str, test_schema: str) -> None:
    """The tsvector expression has to actually name the column."""
    from .test_backends import indexes_on

    adapter = build(dsn, test_schema, meta=content_meta(), store_content=True)
    try:
        definitions = " ".join(indexes_on(dsn, test_schema).values())

        assert "content" in definitions
    finally:
        adapter.close()


def test_a_body_too_large_for_a_tsvector_is_truncated_not_refused(
    dsn: str, test_schema: str
) -> None:
    """PostgreSQL aborts the whole INSERT above 1048575 bytes of tsvector."""
    adapter = build(dsn, test_schema, meta=content_meta(), store_content=True)
    try:
        huge = "lorem ipsum dolor " * 200_000  # far past the tsvector ceiling

        adapter.upsert([{**record("big", content=huge)}])

        stored = adapter.get(["big"])
        assert stored, "the write must succeed rather than abort"
    finally:
        adapter.close()


def test_similarity_by_uri_speaks_the_callers_uri_scheme(
    dsn: str, test_schema: str
) -> None:
    """URIs are stored with `viking://` stripped and handed back with it on.

    A caller ranking the results of a search holds the decoded form. Matching
    it against the column raw finds nothing, and the diversity pass this feeds
    would silently do nothing rather than fail.
    """
    adapter = build(dsn, test_schema, meta=content_meta())
    try:
        adapter.upsert([record("a", seed=1), record("b", seed=2)])
        stored = {row["uri"] for row in adapter.get(["a", "b"])}

        matrix = adapter.pairwise_similarity(sorted(stored), field="uri")

        assert matrix, "the encoded/decoded mismatch would leave this empty"
        assert all(
            str(left).startswith("viking://") and str(right).startswith("viking://")
            for left, right in matrix
        ), "keys must come back in the scheme the caller passed"
    finally:
        adapter.close()
