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
from collections.abc import Callable, Iterator
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
def backend(
    postgres_dsn: str, attach_vector_config: Callable[[Any, int], Any]
) -> Iterator[Any]:
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
    yield attach_vector_config(store, DIM)


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
    """Consumers re-sort by `.score`, which must agree with the order.

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


class _AclOn:
    """An ACL manager reporting ACL as enabled for every account."""

    async def is_enabled(self, account_id: str) -> bool:
        return True


async def test_the_keyword_leg_hides_a_restricted_resource_when_acl_is_on(
    backend: Any,
) -> None:
    """With ACL on, the keyword leg finds what the user may read and nothing else.

    The two documents match the query equally; one is restricted to another
    user. Two layers keep it out: the scope ov-ext builds with OpenViking's
    ``_build_scope_filter``, and the tenant filter OpenViking's own
    ``search_by_keywords`` adds for ``ctx``. This proves the leg runs end to
    end on OpenViking 0.5 and does not leak; ov-ext 0.7 raised on the missing
    ``acl_enabled`` and the leg returned nothing at all. The scope's own ACL
    filter is checked by ``test_the_keyword_leg_runs_against_openvikings_scope_builder``.
    """
    from openviking.storage.acl import EMPTY_ACL_GRANT, AclAction, acl_grant_tokens
    from openviking.storage.collection_schemas import CollectionSchemas

    from ov_ext.retrieval.config import HybridSettings
    from ov_ext.retrieval.retriever import HybridRetriever

    ctx = request_context()
    assert await backend.create_collection(
        COLLECTION, CollectionSchemas.context_collection(COLLECTION, DIM)
    )
    common = {
        "account_id": "acct",
        "context_type": "resource",
        "level": 2,
        "abstract": "intermediate certificate rotation",
        "acl_inherited_grants": [EMPTY_ACL_GRANT],
    }
    # Written through the adapter: OpenViking's own write path resets the ACL
    # fields, which only its ACL manager may set.
    backend._shared_adapter.upsert(
        [
            {
                **common,
                "id": "open",
                "uri": "viking://resources/open.md",
                "vector": NEAR_A,
                "acl_mode": "none",
                "acl_direct_grants": [EMPTY_ACL_GRANT],
            },
            {
                **common,
                "id": "secret",
                "uri": "viking://resources/secret.md",
                "vector": NEAR_B,
                "acl_mode": "restricted",
                "acl_direct_grants": acl_grant_tokens(
                    ["user:someone-else"], AclAction.READ
                )[:1],
            },
        ]
    )
    backend.acl_manager = _AclOn()
    retriever = HybridRetriever(
        settings=HybridSettings(), storage=backend, embedder=StubEmbedder(NEAR_A)
    )

    found = await retriever._keyword_uris(
        text="intermediate certificate",
        ctx=ctx,
        pool=10,
        scope_dsl=None,
        level=None,
        context_type=None,
        target_directories=None,
    )

    assert found == ["viking://resources/open.md"]
    await backend.close()


class _LowScoringReranker:
    """A rerank client whose scores all sit below the held-back cosines."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def rerank_batch(self, query: str, documents: list[str]) -> list[float]:
        self.batches.append(list(documents))
        return [0.05 + 0.01 * index for index in range(len(documents))]


async def test_with_a_document_cap_every_reranked_result_outranks_every_held_back_one(
    backend: Any,
) -> None:
    """The cap must not let the weakest candidates jump the reranked ones.

    Four documents with falling cosines; the cap sends the two strongest, and
    the model scores them low. Held back on their cosines, the other two would
    sort above them -- OpenViking ranks the whole list by score.
    """
    from openviking_cli.retrieve.types import TypedQuery

    from ov_ext.retrieval.config import HybridSettings
    from ov_ext.retrieval.retriever import HybridRetriever

    ctx = request_context()
    assert await backend.create_collection(COLLECTION, schema())
    vectors = {
        "d1": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "d2": [0.9, 0.44, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "d3": [0.8, 0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "d4": [0.7, 0.71, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    }
    await backend.upsert_many(
        [
            record(key, uri=f"viking://resources/{key}", vector=vector, name=f"doc {key}")
            for key, vector in vectors.items()
        ],
        ctx=ctx,
    )
    retriever = HybridRetriever(
        settings=HybridSettings(
            keyword_enabled=False, mmr_enabled=False, rerank_max_documents=2
        ),
        storage=backend,
        embedder=StubEmbedder(NEAR_A),
    )
    reranker = _LowScoringReranker()
    retriever._rerank_client = reranker

    result = await retriever.retrieve(
        TypedQuery(query="doc", context_type=None, intent="", target_directories=[]),
        ctx=ctx,
        limit=4,
    )

    order = [context.uri.rsplit("/", 1)[-1] for context in result.matched_contexts]
    assert reranker.batches == [["doc d1", "doc d2"]]
    assert set(order[:2]) == {"d1", "d2"}, order
    assert order[2:] == ["d3", "d4"], "held back, in their own vector order"
    await backend.close()


class _DedicatedVectorConfig:
    """Resolve one account to a vector database of its own, as OpenViking 0.5 allows."""

    def __init__(self, dedicated: Any) -> None:
        self._dedicated = dedicated

    async def resolve(self, account_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(dedicated_vectordb=True, vectordb=self._dedicated)


async def test_diversity_works_for_an_account_with_a_dedicated_vector_database(
    postgres_dsn: str,
) -> None:
    """Similarity comes from the account's own collection, not the shared one.

    Looked up in the shared collection, which holds none of this account's
    rows, the embedding signal is empty and MMR quietly does nothing.
    """
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    def config(schema_name: str) -> Any:
        return VectorDBBackendConfig(
            backend="ov_postgres.adapter.PgVectorCollectionAdapter",
            name=COLLECTION,
            index_name="default",
            dimension=DIM,
            distance_metric="cosine",
            custom_params={
                "dsn": postgres_dsn,
                "schema": schema_name,
                "store_content": True,
                "text_search_config": "english",
            },
        )

    store = VikingVectorIndexBackend(config(f"shared_{uuid.uuid4().hex[:10]}"))
    store.set_vector_config_resolver(
        _DedicatedVectorConfig(config(f"dedicated_{uuid.uuid4().hex[:10]}"))
    )
    try:
        ctx = request_context()
        dedicated = await store.get_account_backend(ctx.account_id)
        assert dedicated._adapter is not store._shared_adapter
        assert await dedicated.create_collection(COLLECTION, schema())
        await store.upsert_many(
            [
                record("first", uri="viking://resources/first", vector=NEAR_A),
                record("twin", uri="viking://resources/twin", vector=NEAR_A),
                record("other", uri="viking://resources/other", vector=NEAR_B),
            ],
            ctx=ctx,
        )

        plain = await retrieve(
            store, ctx, query="", limit=3, keyword_enabled=False, mmr_enabled=False
        )
        diverse = await retrieve(
            store, ctx, query="", limit=3, keyword_enabled=False, mmr_lambda=0.5
        )

        assert [m.uri for m in plain][:2] == [
            "viking://resources/first",
            "viking://resources/twin",
        ], "without MMR the twins hold both top slots"
        assert diverse[1].uri.endswith("other"), "MMR must break the pair up"
    finally:
        await store.close()
