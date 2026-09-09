"""End-to-end proof that hybrid retrieval works inside real OpenViking.

Everything here is real except the embedding model: a real
``VikingVectorIndexBackend``, the real ``ov_postgres`` adapter, a real
PostgreSQL with pgvector in a container, OpenViking's real
``HierarchicalRetriever``, and the real ``HybridRetriever`` on top of it.

The embedder is a stub, deliberately. It is the one true external boundary --
a paid or heavyweight model -- and stubbing it is also what makes the test
*prove* something: with embeddings under the test's control, the vector leg can
be made deliberately wrong, so a passing assertion can only be explained by the
keyword leg and the diversity pass doing their jobs.

These are the tests that would have caught the two defects the first review
found: the level-suffix mismatch that made both features a no-op for L0 and L1
results, and the stale ``score`` that let downstream consumers re-sort the
fusion away.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

# The backend under test. A dev dependency, not a runtime one: this package
# works with any OpenViking backend and degrades where one cannot do keywords
# or pairwise similarity. Only *proving* it works needs a real database.
pytest.importorskip(
    "ov_postgres", reason="the end-to-end suite needs the PostgreSQL backend"
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DIM = 8
COLLECTION = "context"

# Two unit vectors 0.6 cosine apart. Deliberately *not* orthogonal: the base
# retriever keeps only results scoring above zero, so a document orthogonal to
# the query is dropped before any of this package's code sees it -- and the
# test would then pass or fail for the wrong reason.
NEAR_A = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
NEAR_B = [0.6, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def schema() -> dict[str, Any]:
    """The subset of OpenViking's context schema this test exercises."""
    return {
        "CollectionName": COLLECTION,
        "Description": "end-to-end hybrid retrieval",
        "Fields": [
            {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
            {"FieldName": "uri", "FieldType": "path"},
            {"FieldName": "context_type", "FieldType": "string"},
            {"FieldName": "level", "FieldType": "int64"},
            {"FieldName": "name", "FieldType": "string"},
            {"FieldName": "abstract", "FieldType": "string"},
            {"FieldName": "content", "FieldType": "text"},
            {"FieldName": "search_tags", "FieldType": "list<string>"},
            {"FieldName": "account_id", "FieldType": "string"},
            {"FieldName": "active_count", "FieldType": "int64"},
            {"FieldName": "vector", "FieldType": "vector", "Dim": DIM},
        ],
        "ScalarIndex": ["uri", "context_type", "level", "account_id", "search_tags"],
    }


class StubEmbedder:
    """Returns a fixed query vector, so the vector ranking is under test control.

    Implements the two methods ``embed_compat`` calls
    (``openviking/models/embedder/base.py:92``), and nothing else.
    """

    supports_multimodal = False

    def __init__(self, query_vector: list[float]) -> None:
        self.query_vector = query_vector

    def prepare_embedding_input(self, content: Any) -> Any:
        """Pass text through; this stub has no multimodal handling to do."""
        return content

    async def embed_async(self, content: Any, *, is_query: bool = False) -> Any:
        """Return the configured vector, whatever was asked for."""
        from openviking.models.embedder.base import EmbedResult

        return EmbedResult(dense_vector=list(self.query_vector), sparse_vector=None)


@pytest.fixture(scope="module")
def postgres_dsn() -> Iterator[str]:
    """A throwaway PostgreSQL with pgvector, shared by this module."""
    explicit = os.environ.get("OV_POSTGRES_TEST_DSN")
    if explicit:
        yield explicit
        return
    try:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - environment without Docker support
        pytest.skip("testcontainers is not installed")

    try:
        # Construction talks to the daemon too, so it belongs inside the guard:
        # outside it, a machine with no Docker running errors the suite instead
        # of skipping it.
        container = PostgresContainer("pgvector/pgvector:pg17", driver=None)
        container.start()
    except Exception as exc:  # pragma: no cover - no container runtime
        pytest.skip(f"cannot start a PostgreSQL container: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def backend(postgres_dsn: str) -> Iterator[Any]:
    """A real OpenViking vector backend on a real PostgreSQL, per test."""
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

    db_schema = f"e2e_{uuid.uuid4().hex[:10]}"
    config = VectorDBBackendConfig(
        backend="ov_postgres.adapter.PgVectorCollectionAdapter",
        name=COLLECTION,
        index_name="default",
        dimension=DIM,
        distance_metric="cosine",
        custom_params={
            "dsn": postgres_dsn,
            "schema": db_schema,
            "store_content": True,
            "text_search_config": "english",
        },
    )
    store = VikingVectorIndexBackend(config)
    yield store


def request_context() -> Any:
    """A request context for a single tenant."""
    from openviking.server.identity import Role, UserIdentifier
    from openviking.server.identity import RequestContext

    return RequestContext(user=UserIdentifier("acct", "user"), role=Role.USER)


def record(
    key: str,
    *,
    uri: str,
    vector: list[float],
    level: int = 2,
    name: str = "",
    content: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """One stored context row."""
    return {
        "id": key,
        "uri": uri,
        "context_type": "resource",
        "level": level,
        "name": name,
        "abstract": name,
        "content": content,
        "search_tags": tags or [],
        "active_count": 0,
        "vector": vector,
    }


async def retrieve(
    store: Any, ctx: Any, *, query: str, limit: int, **settings: Any
) -> list[Any]:
    """Run a hybrid retrieval and return the matched contexts."""
    from openviking_cli.retrieve.types import TypedQuery

    from ov_ext.retrieval.config import HybridSettings
    from ov_ext.retrieval.retriever import HybridRetriever

    retriever = HybridRetriever(
        settings=HybridSettings(**settings),
        storage=store,
        embedder=StubEmbedder(NEAR_A),
    )
    result = await retriever.retrieve(
        TypedQuery(query=query, context_type=None, intent="", target_directories=[]),
        ctx=ctx,
        limit=limit,
    )
    return list(result.matched_contexts)


async def test_the_keyword_leg_promotes_a_document_the_vector_leg_ranks_last(
    backend: Any,
) -> None:
    """The whole point: an exact term the embedding misses still surfaces.

    The query vector is NEAR_A, so "decoy" ranks first by similarity and
    "buried" ranks last. Only "buried" contains the searched phrase, and only
    in its body -- so promoting it can only be the keyword leg's doing.
    """
    ctx = request_context()
    assert await backend.create_collection(COLLECTION, schema())
    await backend.upsert_many(
        [
            record(
                "decoy",
                uri="viking://resources/decoy",
                vector=NEAR_A,
                name="Unrelated notes",
                content="Nothing of interest in this document.",
            ),
            record(
                "buried",
                uri="viking://resources/buried",
                vector=NEAR_B,
                name="Unrelated notes",
                content="Rotating the intermediate certificate authority by hand.",
            ),
        ],
        ctx=ctx,
    )

    vector_only = await retrieve(
        backend, ctx, query="intermediate certificate", limit=2, keyword_enabled=False
    )
    hybrid = await retrieve(
        backend, ctx, query="intermediate certificate", limit=2, mmr_enabled=False
    )

    assert vector_only[0].uri.endswith("decoy"), (
        "the vector leg alone ranks the decoy first"
    )
    assert hybrid[0].uri.endswith("buried"), "the keyword leg must promote the real match"

    await backend.close()


async def test_hybrid_ranking_survives_a_downstream_re_sort_by_score(
    backend: Any,
) -> None:
    """`/skills/search` re-sorts by `.score`, which must agree with the order.

    A ranking that lives only in list order is discarded by any consumer that
    sorts, which is how the fusion was being silently undone.
    """
    ctx = request_context()
    assert await backend.create_collection(COLLECTION, schema())
    await backend.upsert_many(
        [
            record(
                "decoy",
                uri="viking://resources/decoy",
                vector=NEAR_A,
                content="Nothing of interest.",
            ),
            record(
                "buried",
                uri="viking://resources/buried",
                vector=NEAR_B,
                content="Rotating the intermediate certificate authority.",
            ),
        ],
        ctx=ctx,
    )

    hybrid = await retrieve(
        backend, ctx, query="intermediate certificate", limit=2, mmr_enabled=False
    )
    by_score = sorted(hybrid, key=lambda m: m.score, reverse=True)

    assert [m.uri for m in by_score] == [m.uri for m in hybrid]

    await backend.close()


async def test_the_keyword_leg_reaches_level_0_results(backend: Any) -> None:
    """L0 URIs are displayed with `/.abstract.md` appended but stored without it.

    Matching the two raw made the keyword leg a silent no-op for every
    directory-level result. Nothing errored; it simply never fired.
    """
    ctx = request_context()
    assert await backend.create_collection(COLLECTION, schema())
    await backend.upsert_many(
        [
            record(
                "decoy",
                uri="viking://resources/decoy",
                vector=NEAR_A,
                level=0,
                content="Nothing of interest.",
            ),
            record(
                "buried",
                uri="viking://resources/buried",
                vector=NEAR_B,
                level=0,
                content="Rotating the intermediate certificate authority.",
            ),
        ],
        ctx=ctx,
    )

    hybrid = await retrieve(
        backend, ctx, query="intermediate certificate", limit=2, mmr_enabled=False
    )

    assert hybrid[0].uri.endswith("/.abstract.md"), "L0 keeps its display suffix"
    assert "buried" in hybrid[0].uri, "the keyword leg must fire for L0 too"

    await backend.close()


async def test_diversity_demotes_a_near_duplicate_end_to_end(backend: Any) -> None:
    """Two rows with the same embedding should not take both top slots."""
    ctx = request_context()
    assert await backend.create_collection(COLLECTION, schema())
    await backend.upsert_many(
        [
            record("first", uri="viking://resources/first", vector=NEAR_A),
            record("twin", uri="viking://resources/twin", vector=NEAR_A),
            record("other", uri="viking://resources/other", vector=NEAR_B),
        ],
        ctx=ctx,
    )

    plain = await retrieve(
        backend, ctx, query="", limit=3, keyword_enabled=False, mmr_enabled=False
    )
    diverse = await retrieve(
        backend, ctx, query="", limit=3, keyword_enabled=False, mmr_lambda=0.5
    )

    assert [m.uri for m in plain][:2] == [
        "viking://resources/first",
        "viking://resources/twin",
    ], "without MMR the twins hold both top slots"
    assert diverse[1].uri.endswith("other"), "MMR must break the pair up"

    await backend.close()


async def test_diversity_reaches_level_0_results(backend: Any) -> None:
    """MMR looks similarity up by the stored URI, not the displayed one.

    The database holds no row whose `uri` ends in `/.abstract.md`, so a
    diversity pass querying the display URI finds nothing and silently stops
    working for every directory-level result -- the mirror of the keyword-leg
    bug, and the half of that fix with no end-to-end cover until now.
    """
    ctx = request_context()
    assert await backend.create_collection(COLLECTION, schema())
    await backend.upsert_many(
        [
            record("first", uri="viking://resources/first", vector=NEAR_A, level=0),
            record("twin", uri="viking://resources/twin", vector=NEAR_A, level=0),
            record("other", uri="viking://resources/other", vector=NEAR_B, level=0),
        ],
        ctx=ctx,
    )

    plain = await retrieve(
        backend, ctx, query="", limit=3, keyword_enabled=False, mmr_enabled=False
    )
    diverse = await retrieve(
        backend, ctx, query="", limit=3, keyword_enabled=False, mmr_lambda=0.5
    )

    assert [m.uri for m in plain][:2] == [
        "viking://resources/first/.abstract.md",
        "viking://resources/twin/.abstract.md",
    ], "without MMR the twins hold both top slots"
    assert diverse[1].uri.endswith("other/.abstract.md"), "MMR must fire for L0 too"

    await backend.close()
