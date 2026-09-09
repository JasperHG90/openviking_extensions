"""Reflection against a real OpenViking, on a real PostgreSQL.

Everything here is real except the model: a real RAGFS filesystem on a temp
directory, a real ``VikingVectorIndexBackend``, the real ``ov_postgres``
adapter, a real PostgreSQL with pgvector in a container, and the real
``VikingStore`` and ``ReflectionEngine`` on top.

The model is scripted, deliberately. It is the one true external boundary, and
scripting it is also what makes these tests *prove* something: with the reply
under the test's control, a written observation can only be explained by the
verification gate and the write path doing their jobs.

These cover the half the unit suite cannot. Every adapter defect the reviews
found was an assumption about OpenViking's API that a fake happily satisfied --
`MemoryFileUtils.serialize`, `MemoryFile(metadata=…)`,
`FindResult.matched_contexts`. The most valuable test here is
``test_reflection_refuses_to_run_when_the_backend_stores_no_content``: it runs
against OpenViking's *default* configuration, where the fallback that used to
verify quotes against a generated summary would have silently engaged.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

pytest.importorskip(
    "ov_postgres", reason="the reflection integration suite needs the PostgreSQL backend"
)

from ov_ext.reflect.config import ReflectSettings  # noqa: E402
from ov_ext.reflect.engine import ReflectionEngine  # noqa: E402
from ov_ext.reflect.exceptions import ContentUnavailableError  # noqa: E402
from ov_ext.reflect.models import (  # noqa: E402
    CandidateObservation,
    Contradictions,
    ContradictionRelationship,
    EvidenceItem,
    Observation,
    ProposedObservations,
)
from ov_ext.reflect.locks import ProcessLock
from ov_ext.reflect.runner import run_sweep  # noqa: E402
from ov_ext.reflect.viking import VikingStore  # noqa: E402
from ov_ext.reflect.watermark import Watermark  # noqa: E402

from .helpers import FakeLLM  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DIM = 8
COLLECTION = "context"
VECTOR = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

USER = "jasper"
ROOT = f"viking://user/{USER}/memories"
A = f"{ROOT}/entities/scheduler.md"
B = f"{ROOT}/entities/worker.md"

A_TEXT = "The scheduler retries failed jobs three times before giving up."
B_TEXT = "The worker retries failed jobs with exponential backoff."


def schema() -> dict[str, Any]:
    """The subset of OpenViking's context schema reflection touches."""
    return {
        "CollectionName": COLLECTION,
        "Description": "reflection integration",
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
            {"FieldName": "created_at", "FieldType": "date_time"},
            {"FieldName": "updated_at", "FieldType": "date_time"},
            {"FieldName": "vector", "FieldType": "vector", "Dim": DIM},
        ],
        "ScalarIndex": [
            "uri",
            "context_type",
            "level",
            "account_id",
            "search_tags",
            "created_at",
            "updated_at",
        ],
    }


class _StubEmbedding:
    """Just the dimension the adapter reads."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension


class _StubConfig:
    """Just the embedding section the adapter reads."""

    def __init__(self, embedding: _StubEmbedding) -> None:
        self.embedding = embedding


@pytest.fixture(autouse=True)
def _fixed_embedding_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the embedding dimension the adapter reads from global config.

    ``CollectionAdapter.query`` builds a placeholder vector for a filter-only
    query, sized from ``get_openviking_config().embedding.dimension``. Left
    alone it reads the developer's real ``~/.openviking/ov.conf`` -- which
    says 512 -- and every filter fails against this suite's 8-dimension
    collection with "different vector dimensions". Mirrors the fixture
    ov-postgres's own suite uses for the same reason.
    """
    from openviking.storage.vectordb_adapters import base

    monkeypatch.setattr(
        base,
        "get_openviking_config",
        lambda: _StubConfig(embedding=_StubEmbedding(dimension=DIM)),
    )


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


def _backend(dsn: str, *, store_content: bool) -> Any:
    """A real OpenViking vector backend on a real PostgreSQL, in its own schema."""
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

    return VikingVectorIndexBackend(
        VectorDBBackendConfig(
            backend="ov_postgres.adapter.PgVectorCollectionAdapter",
            name=COLLECTION,
            index_name="default",
            dimension=DIM,
            distance_metric="cosine",
            custom_params={
                "dsn": dsn,
                "schema": f"reflect_{uuid.uuid4().hex[:10]}",
                "store_content": store_content,
                "text_search_config": "english",
            },
        )
    )


@pytest.fixture
def backend(postgres_dsn: str) -> Iterator[Any]:
    """A backend that stores row content, which reflection requires."""
    yield _backend(postgres_dsn, store_content=True)


@pytest.fixture
def contentless_backend(postgres_dsn: str) -> Iterator[Any]:
    """A backend configured the way OpenViking defaults: no stored content."""
    yield _backend(postgres_dsn, store_content=False)


@pytest.fixture
def viking_fs() -> Iterator[Any]:
    """A real RAGFS filesystem over a temporary directory."""
    from openviking_cli.utils.config.agfs_config import AGFSConfig

    from openviking.storage.viking_fs import VikingFS
    from openviking.utils.agfs_utils import RagfsBindingConfig, create_agfs_client

    with tempfile.TemporaryDirectory() as workspace:
        client = create_agfs_client(
            RagfsBindingConfig(agfs=AGFSConfig(backend="local", path=workspace))
        )
        yield VikingFS(agfs=client)


def ctx() -> Any:
    """A request context for one tenant."""
    from openviking.server.identity import RequestContext, Role, UserIdentifier

    return RequestContext(user=UserIdentifier("acct", USER), role=Role.USER)


def memory_row(key: str, *, uri: str, text: str, when: datetime) -> dict[str, Any]:
    """One indexed memory row as OpenViking stores it.

    Timestamps go in as ISO strings, which is what the `date_time` field
    accepts: the backend coerces them to epoch milliseconds on the way in and
    hands the integer back on the way out.
    """
    return {
        "id": key,
        "uri": uri,
        "context_type": "memory",
        "level": 2,
        "name": uri.rsplit("/", 1)[-1],
        "abstract": "a generated summary that is NOT the memory text",
        "content": text,
        "search_tags": [],
        "account_id": "acct",
        "active_count": 0,
        "created_at": when.isoformat(),
        "updated_at": when.isoformat(),
        "vector": VECTOR,
    }


async def seed(store: Any, when: datetime) -> None:
    """Create the collection and insert two changed memories."""
    assert await store.create_collection(COLLECTION, schema())
    await store.upsert_many(
        [
            memory_row("a", uri=A, text=A_TEXT, when=when),
            memory_row("b", uri=B, text=B_TEXT, when=when),
        ],
        ctx=ctx(),
    )


def settings(**overrides: Any) -> ReflectSettings:
    """Settings with reflection on and the environment ignored."""
    base = ReflectSettings().model_dump()
    base.update({"enabled": True, "neighbour_limit": 0, "tail_sample": 0})
    base.update(overrides)
    return ReflectSettings.model_construct(**base)


def proposal() -> ProposedObservations:
    """A model reply citing both seeded memories, with quotes that really exist."""
    return ProposedObservations(
        observations=[
            CandidateObservation(
                title="Both components retry",
                content="The scheduler and the worker both retry failed jobs.",
                evidence=[
                    EvidenceItem(
                        memory_index=0,
                        quote="retries failed jobs",
                        relevance_explanation="states the behaviour",
                    ),
                    EvidenceItem(
                        memory_index=1,
                        quote="exponential backoff",
                        relevance_explanation="states how",
                    ),
                ],
            )
        ]
    )


# --- the adapter, against the real store -----------------------------------


async def test_changed_since_finds_real_rows_and_orders_them_oldest_first(
    backend: Any, viking_fs: Any
) -> None:
    """Proves the filter expressions, output fields and ordering against real SQL."""
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    new = datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert await backend.create_collection(COLLECTION, schema())
    await backend.upsert_many(
        [
            memory_row("b", uri=B, text=B_TEXT, when=new),
            memory_row("a", uri=A, text=A_TEXT, when=old),
        ],
        ctx=ctx(),
    )

    store = VikingStore(viking_fs, backend, ctx(), settings())
    found = await store.changed_since(datetime(2026, 8, 1, tzinfo=timezone.utc), limit=10)

    assert found == [A, B]
    await backend.close()


async def test_changed_since_excludes_everything_at_or_before_the_mark(
    backend: Any, viking_fs: Any
) -> None:
    """TimeRange compiles `start` to `>=`, so the boundary row is dropped here.

    Left in, it returns every sweep and spends a batch slot; enough rows sharing
    one timestamp and the watermark could never advance past them.
    """
    when = datetime(2026, 9, 5, tzinfo=timezone.utc)
    await seed(backend, when)

    store = VikingStore(viking_fs, backend, ctx(), settings())

    assert await store.changed_since(when, limit=10) == []
    assert set(
        await store.changed_since(datetime(2026, 9, 4, tzinfo=timezone.utc), limit=10)
    ) == {A, B}
    await backend.close()


async def test_rows_return_the_memory_text_not_the_abstract(
    backend: Any, viking_fs: Any
) -> None:
    """Quotes are verified against this, so it must be the memory itself."""
    await seed(backend, datetime(2026, 9, 5, tzinfo=timezone.utc))
    store = VikingStore(viking_fs, backend, ctx(), settings())

    rows = await store.rows([A, B])

    assert {row.text for row in rows} == {A_TEXT, B_TEXT}
    assert all("generated summary" not in row.text for row in rows)
    await backend.close()


async def test_reflection_refuses_to_run_when_the_backend_stores_no_content(
    contentless_backend: Any, viking_fs: Any
) -> None:
    """The default configuration, and the one that used to fail silently.

    With `store_content` off OpenViking drops `content` at write time. The old
    fallback to `abstract` meant the model was shown a summary, quoted the
    summary, and the quote verified against the summary -- while the link
    written from it claimed a `match_text` that appears nowhere in the memory.
    """
    await seed(contentless_backend, datetime(2026, 9, 5, tzinfo=timezone.utc))
    store = VikingStore(viking_fs, contentless_backend, ctx(), settings())

    with pytest.raises(ContentUnavailableError, match="store_content"):
        await store.rows([A, B])

    await contentless_backend.close()


async def test_tail_sample_returns_real_rows(backend: Any, viking_fs: Any) -> None:
    await seed(backend, datetime(2026, 9, 5, tzinfo=timezone.utc))
    store = VikingStore(viking_fs, backend, ctx(), settings())

    sampled = await store.tail_sample(limit=2)

    assert {row.uri for row in sampled} == {A, B}
    await backend.close()


# --- the write path, against the real filesystem ---------------------------


async def test_an_observation_is_written_as_a_memory_openviking_can_parse(
    backend: Any, viking_fs: Any
) -> None:
    """The three adapter defects all lived on this path and all failed silently."""
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    store = VikingStore(viking_fs, backend, ctx(), settings())
    observation = Observation(
        title="Both components retry",
        content="The scheduler and the worker both retry failed jobs.",
        evidence=((A, "retries failed jobs"), (B, "exponential backoff")),
        areas=frozenset({f"{ROOT}/entities"}),
    )

    uri = await store.write_observation(observation)
    parsed = MemoryFileUtils.read(await viking_fs.read_file(uri, ctx=ctx()), uri=uri)

    assert {link["to_uri"] for link in parsed.links} == {A, B}
    assert {link["link_type"] for link in parsed.links} == {"derived_from"}
    assert {link["match_text"] for link in parsed.links} == {
        "retries failed jobs",
        "exponential backoff",
    }
    assert parsed.memory_type == "observations"


async def test_a_contradiction_link_lands_on_a_real_memory_file(
    backend: Any, viking_fs: Any
) -> None:
    """The one write that touches a file reflection did not author."""
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    await viking_fs.write_file(A, f"# scheduler\n\n{A_TEXT}\n", ctx=ctx())
    store = VikingStore(viking_fs, backend, ctx(), settings())

    await store.link(A, B, link_type="contradicts", weight=0.9)
    parsed = MemoryFileUtils.read(await viking_fs.read_file(A, ctx=ctx()), uri=A)

    assert [link["to_uri"] for link in parsed.links] == [B]
    assert parsed.links[0]["link_type"] == "contradicts"
    # The body survives, because the link is merged in rather than replacing it.
    assert A_TEXT in parsed.content


async def test_a_missing_overview_reads_as_absent_not_as_an_error(
    backend: Any, viking_fs: Any
) -> None:
    store = VikingStore(viking_fs, backend, ctx(), settings())
    assert await store.read_overview(f"{ROOT}/entities") is None


# --- the whole sweep -------------------------------------------------------


async def test_a_sweep_writes_an_observation_and_remembers_where_it_got_to(
    backend: Any, viking_fs: Any
) -> None:
    """End to end: real index in, real memory file and real watermark out."""
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    when = datetime(2026, 9, 5, tzinfo=timezone.utc)
    await seed(backend, when)
    llm = FakeLLM([proposal(), Contradictions(relationships=[])])

    report = await run_sweep(
        viking_fs,
        backend,
        ctx(),
        settings(),
        llm=llm,
        now=datetime.now(timezone.utc),
        lock=ProcessLock(),
    )

    assert report.written == 1
    assert report.dropped["quote_not_found"] == 0

    listed = await viking_fs.ls(f"viking://user/{USER}/memories/observations", ctx=ctx())
    assert listed, "the observation should be on disk"

    state = f"viking://user/{USER}/resources/reflect/watermark.json"
    stored = Watermark.loads(await viking_fs.read_file(state, ctx=ctx()))
    assert stored.last_seen == when

    # And a second sweep finds nothing left to do.
    again = await run_sweep(
        viking_fs,
        backend,
        ctx(),
        settings(),
        llm=FakeLLM([]),
        now=datetime.now(timezone.utc),
        lock=ProcessLock(),
    )
    assert again.batches == 0
    await backend.close()

    del MemoryFileUtils  # imported for symmetry with the sibling tests


async def test_a_fabricated_quote_never_reaches_the_real_store(
    backend: Any, viking_fs: Any
) -> None:
    """The gate, proven against a real filesystem rather than a recording fake."""
    await seed(backend, datetime(2026, 9, 5, tzinfo=timezone.utc))
    invented = ProposedObservations(
        observations=[
            CandidateObservation(
                title="Both components retry",
                content="Something no memory says.",
                evidence=[
                    EvidenceItem(
                        memory_index=0,
                        quote="retries seventeen times",
                        relevance_explanation="fabricated",
                    ),
                    EvidenceItem(
                        memory_index=1,
                        quote="exponential backoff",
                        relevance_explanation="real",
                    ),
                ],
            )
        ]
    )
    store = VikingStore(viking_fs, backend, ctx(), settings())
    engine = ReflectionEngine(
        store, FakeLLM([invented, Contradictions(relationships=[])]), settings()
    )

    report, _ = await engine.sweep(Watermark.beginning(), now=datetime.now(timezone.utc))

    assert report.proposed == 1
    assert report.written == 0
    assert report.dropped["quote_not_found"] == 1
    with pytest.raises(Exception):
        await viking_fs.ls(f"viking://user/{USER}/memories/observations", ctx=ctx())
    await backend.close()


async def test_a_contradiction_found_in_a_sweep_lands_on_the_memory(
    backend: Any, viking_fs: Any
) -> None:
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    await seed(backend, datetime(2026, 9, 5, tzinfo=timezone.utc))
    await viking_fs.write_file(A, f"# scheduler\n\n{A_TEXT}\n", ctx=ctx())
    llm = FakeLLM(
        [
            ProposedObservations(observations=[]),
            Contradictions(
                relationships=[
                    ContradictionRelationship(
                        left_index=0,
                        right_index=1,
                        relation="contradict",
                        reasoning="different retry policies",
                    )
                ]
            ),
        ]
    )
    store = VikingStore(viking_fs, backend, ctx(), settings())
    engine = ReflectionEngine(store, llm, settings())

    report, _ = await engine.sweep(Watermark.beginning(), now=datetime.now(timezone.utc))

    assert report.contradictions == 1
    parsed = MemoryFileUtils.read(await viking_fs.read_file(A, ctx=ctx()), uri=A)
    assert [link["link_type"] for link in parsed.links] == ["contradicts"]
    await backend.close()


# --- the sweep lock, against a real Postgres --------------------------------
#
# The guarantee is "two processes never sweep at once". A lock tested against a
# fake proves only that the fake agrees with itself, so every assertion below
# runs against a real database, and the mutation note on each says what breaks
# if the lock is removed.


async def test_only_one_holder_gets_the_lock(postgres_dsn: str) -> None:
    """Two independent locks, two connections, one winner.

    This is the guarantee in one assertion. Delete the pg_try_advisory_lock
    call and both return True.
    """
    from ov_ext.reflect.locks import PostgresAdvisoryLock

    first = PostgresAdvisoryLock(postgres_dsn)
    second = PostgresAdvisoryLock(postgres_dsn)

    async with first.acquire() as held:
        assert held is True
        async with second.acquire() as also:
            assert also is False


async def test_the_lock_is_released_when_the_holder_lets_go(
    postgres_dsn: str,
) -> None:
    from ov_ext.reflect.locks import PostgresAdvisoryLock

    lock = PostgresAdvisoryLock(postgres_dsn)
    async with lock.acquire() as held:
        assert held is True
    async with PostgresAdvisoryLock(postgres_dsn).acquire() as again:
        assert again is True


async def test_the_lock_is_released_when_the_connection_dies(
    postgres_dsn: str,
) -> None:
    """The property that makes this better than a TTL lock.

    A Redis lease expires on a timer, so a holder that stalls past its TTL
    keeps believing it holds a lock somebody else now has. An advisory lock is
    bound to the session: kill the connection and the lock goes with it, with
    no timeout to wait out and no clock to trust.

    Killing the backend from another connection is as close to `kill -9` as a
    test can get without forking.
    """
    import psycopg

    from ov_ext.reflect.locks import PostgresAdvisoryLock, advisory_key

    victim = psycopg.connect(postgres_dsn, autocommit=True)
    with victim.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (advisory_key(),))
        taken = cur.fetchone()
        assert taken is not None and taken[0] is True
        cur.execute("SELECT pg_backend_pid()")
        backend = cur.fetchone()
        assert backend is not None
        pid = backend[0]

    # Contended while the holder lives.
    async with PostgresAdvisoryLock(postgres_dsn).acquire() as contended:
        assert contended is False

    executioner = psycopg.connect(postgres_dsn, autocommit=True)
    with executioner.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
    executioner.close()

    # Free the moment the connection is gone -- no TTL, no waiting.
    async with PostgresAdvisoryLock(postgres_dsn).acquire() as freed:
        assert freed is True


async def test_a_raising_sweep_still_releases_the_lock(postgres_dsn: str) -> None:
    """Otherwise one failed sweep wedges reflection until the process restarts."""
    from ov_ext.reflect.locks import PostgresAdvisoryLock

    lock = PostgresAdvisoryLock(postgres_dsn)
    with pytest.raises(RuntimeError):
        async with lock.acquire() as held:
            assert held is True
            raise RuntimeError("sweep exploded")

    async with PostgresAdvisoryLock(postgres_dsn).acquire() as again:
        assert again is True


async def test_two_concurrent_sweeps_produce_one_sweep(
    backend: Any, viking_fs: Any, postgres_dsn: str
) -> None:
    """The guarantee where it matters: at the watermark.

    Both callers are handed a model that would write an observation. Without
    the lock both write, both advance the mark, and each consumes work the
    other was half-way through. With it, exactly one runs -- and the loser
    reports nothing rather than failing, which is what a follower should do.
    """
    from ov_ext.reflect.locks import PostgresAdvisoryLock

    when = datetime(2026, 9, 5, tzinfo=timezone.utc)
    await seed(backend, when)

    async def sweep() -> Any:
        # A lock of its own, so this is two independent processes as far as
        # Postgres is concerned -- not two callers sharing one object.
        return await run_sweep(
            viking_fs,
            backend,
            ctx(),
            settings(),
            lock=PostgresAdvisoryLock(postgres_dsn),
            llm=FakeLLM([proposal(), Contradictions(relationships=[])]),
            now=datetime.now(timezone.utc),
        )

    first, second = await asyncio.gather(sweep(), sweep())

    written = sorted([first.written, second.written])
    assert written == [0, 1], "exactly one sweep should have written"
    on_disk = await viking_fs.ls(f"viking://user/{USER}/memories/observations", ctx=ctx())
    assert len(on_disk) == 1
    await backend.close()


async def test_a_sweep_whose_lock_connection_is_reaped_is_cancelled(
    postgres_dsn: str,
) -> None:
    """The hole the review found, and the heartbeat that closes it.

    The lock connection is idle for the whole sweep, which is what a reaper
    acts on. Before the heartbeat, `idle_session_timeout` dropped it mid-sweep
    and a second caller took the lock while the first was still going -- the
    exact double-sweep the TTL argument rejects Redis for.

    The lock now disables the timeout for its own session, so this test forces
    the loss the other way, by terminating the backend. What must happen is not
    that the lock survives, but that the sweep holding it stops.
    """
    import psycopg

    from ov_ext.reflect.locks import PostgresAdvisoryLock

    lock = PostgresAdvisoryLock(postgres_dsn, heartbeat_seconds=0.2)
    swept_on = False

    async def sweep_under_lock() -> None:
        nonlocal swept_on
        async with lock.acquire() as held:
            assert held is True
            await asyncio.sleep(3)
            # Only reached if nothing stopped us after the lock was lost.
            swept_on = True

    task = asyncio.create_task(sweep_under_lock())
    await asyncio.sleep(0.5)

    # Kill every backend but our own that holds this key, the way a failover or
    # an admin would.
    killer = psycopg.connect(postgres_dsn, autocommit=True)
    with killer.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid <> pg_backend_pid()"
        )
    killer.close()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert swept_on is False, "the sweep must stop when its lock is gone"


async def test_the_lock_session_refuses_to_be_reaped_for_being_idle(
    postgres_dsn: str,
) -> None:
    """Prevention, not just detection.

    With `idle_session_timeout` left alone, a sweep longer than the server's
    timeout loses its lock. The lock sets the timeout to 0 for its own session,
    so a long sweep keeps it.
    """
    import psycopg

    from ov_ext.reflect.locks import PostgresAdvisoryLock

    admin = psycopg.connect(postgres_dsn, autocommit=True)
    with admin.cursor() as cur:
        cur.execute("ALTER SYSTEM SET idle_session_timeout = '400ms'")
        cur.execute("SELECT pg_reload_conf()")
    try:
        lock = PostgresAdvisoryLock(postgres_dsn, heartbeat_seconds=5)
        async with lock.acquire() as held:
            assert held is True
            # Several times the server's timeout, with the connection idle.
            await asyncio.sleep(1.5)
            async with PostgresAdvisoryLock(postgres_dsn).acquire() as other:
                assert other is False, "the lock was reaped mid-sweep"
    finally:
        with admin.cursor() as cur:
            cur.execute("ALTER SYSTEM RESET idle_session_timeout")
            cur.execute("SELECT pg_reload_conf()")
        admin.close()
