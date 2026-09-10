"""The memory-delta table: what it creates, and what it does with rows.

The DDL shape is checked without a database; everything that matters about
behaviour -- ordering, idempotence, the merge remap -- runs against a real
PostgreSQL, because those are properties of the SQL rather than of this code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from ov_postgres import ddl
from ov_postgres.config import PgVectorParams
from ov_postgres.deltas import DELTA_TABLE, PgDeltaStore, delta_table_statements

EPOCH = datetime(2026, 9, 10, tzinfo=timezone.utc)
A = "viking://user/jasper/memories/entities/dev_tool/ov_ext.md"
B = "viking://user/jasper/memories/entities/dev_tool/ov_dash.md"


@dataclass(frozen=True)
class Delta:
    """A captured change, shaped like ov-ext's ``MemoryDelta``.

    Declared here rather than imported: the two packages agree on a shape and
    neither depends on the other, so a test that imported ov-ext would prove
    less than this one does.
    """

    uri: str
    memory_type: str
    field: str
    search: str
    replace: str
    created: bool
    changed_at: datetime


def delta(
    uri: str = A, *, search: str = "old", replace: str = "new", day: int = 0
) -> Delta:
    """Build one captured change, ``day`` days after the epoch."""
    return Delta(
        uri=uri,
        memory_type="entities",
        field="content",
        search=search,
        replace=replace,
        created=False,
        changed_at=EPOCH + timedelta(days=day),
    )


# --- the DDL, without a database -------------------------------------------


def test_the_delta_table_is_absent_unless_asked_for() -> None:
    """Nothing in this package reads it, so it must not appear by default."""
    rendered = " ".join(
        statement.as_string(None) for statement in ddl.bootstrap_statements("s")
    )
    assert DELTA_TABLE not in rendered


def test_keep_deltas_adds_the_table_to_the_bootstrap() -> None:
    """The flag is what puts the table in the bootstrap transaction."""
    rendered = " ".join(
        statement.as_string(None)
        for statement in ddl.bootstrap_statements("s", keep_deltas=True)
    )
    assert DELTA_TABLE in rendered
    assert "CREATE TABLE IF NOT EXISTS" in rendered


def test_every_delta_statement_is_idempotent() -> None:
    """The bootstrap runs on every start, against a table that may hold rows."""
    for statement in delta_table_statements("s"):
        rendered = statement.as_string(None)
        assert "IF NOT EXISTS" in rendered, rendered


def test_the_pending_index_covers_only_unreflected_rows() -> None:
    """Partial, so it shrinks as the sweep drains rather than growing with history."""
    rendered = " ".join(s.as_string(None) for s in delta_table_statements("s"))
    assert "WHERE reflected_at IS NULL" in rendered


def test_keep_deltas_is_off_by_default() -> None:
    """A deployment without the reflect sweep should not carry the table."""
    assert PgVectorParams().keep_deltas is False


def test_keep_deltas_is_read_from_custom_params() -> None:
    """It arrives through ov.conf like every other backend setting."""
    assert PgVectorParams.model_validate({"keep_deltas": True}).keep_deltas is True


# --- behaviour, against a real PostgreSQL ----------------------------------


@pytest.fixture
def store(dsn: str, test_schema: str) -> Iterator[PgDeltaStore]:
    """Yield a delta store on a throwaway schema, table already created."""
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True)
    with pool.connection() as conn, conn.cursor() as cur:
        for statement in delta_table_statements(test_schema):
            cur.execute(statement)
        conn.commit()
    try:
        yield PgDeltaStore(pool, test_schema)
    finally:
        pool.close()


@pytest.mark.integration
def test_a_recorded_delta_comes_back_pending(store: PgDeltaStore) -> None:
    """The round trip: what was written is what comes back."""
    asyncio.run(store.record([delta()]))

    pending = store.pending(limit=10)

    assert len(pending) == 1
    assert pending[0]["uri"] == A
    assert pending[0]["search"] == "old"
    assert pending[0]["replace"] == "new"
    assert pending[0]["memory_type"] == "entities"


@pytest.mark.integration
def test_an_empty_batch_writes_nothing(store: PgDeltaStore) -> None:
    """A sweep with nothing to record must not touch a connection."""
    asyncio.run(store.record([]))
    assert store.pending(limit=10) == []


@pytest.mark.integration
def test_pending_returns_oldest_first(store: PgDeltaStore) -> None:
    """A limit must truncate the newest, so the rest survives to the next sweep."""
    asyncio.run(store.record([delta(day=5), delta(day=1), delta(day=3)]))

    pending = store.pending(limit=2)

    # EPOCH is the 10th, so days 1, 3 and 5 are the 11th, 13th and 15th. The
    # limit must drop the 15th, not the 11th.
    assert [row["changed_at"].day for row in pending] == [11, 13]


@pytest.mark.integration
def test_marking_reflected_removes_a_delta_from_pending(
    store: PgDeltaStore,
) -> None:
    """Draining is what stops the sweep re-reading the same change."""
    asyncio.run(store.record([delta(search="a"), delta(search="b")]))
    pending = store.pending(limit=10)

    moved = store.mark_reflected([pending[0]["id"]], when=EPOCH)

    assert moved == 1
    assert [row["search"] for row in store.pending(limit=10)] == ["b"]


@pytest.mark.integration
def test_marking_the_same_delta_twice_is_harmless(store: PgDeltaStore) -> None:
    """What an interrupted sweep looks like when it runs again."""
    asyncio.run(store.record([delta()]))
    ids = [row["id"] for row in store.pending(limit=10)]

    assert store.mark_reflected(ids, when=EPOCH) == 1
    assert store.mark_reflected(ids, when=EPOCH) == 0


@pytest.mark.integration
def test_a_merge_moves_the_dead_uris_history_onto_the_survivor(
    store: PgDeltaStore,
) -> None:
    """Otherwise one memory's changes end up split across two keys."""
    asyncio.run(
        store.record([delta(uri=A, search="from a"), delta(uri=B, search="from b")])
    )

    moved = store.remap_uri(A, B)

    assert moved == 1
    assert {row["uri"] for row in store.pending(limit=10)} == {B}


@pytest.mark.integration
def test_remapping_a_uri_onto_itself_changes_nothing(
    store: PgDeltaStore,
) -> None:
    """A no-op merge must not rewrite rows."""
    asyncio.run(store.record([delta(uri=A)]))
    assert store.remap_uri(A, A) == 0
    assert len(store.pending(limit=10)) == 1


@pytest.mark.integration
def test_forgetting_a_memory_drops_its_deltas(store: PgDeltaStore) -> None:
    """A deleted memory's changes quote text nobody can read any more."""
    asyncio.run(store.record([delta(uri=A), delta(uri=B)]))

    assert store.forget(A) == 1
    assert {row["uri"] for row in store.pending(limit=10)} == {B}


@pytest.mark.integration
def test_since_bounds_the_window(store: PgDeltaStore) -> None:
    """A caller replaying a window can bound what it reads."""
    asyncio.run(store.record([delta(day=1), delta(day=5)]))

    pending = store.pending(limit=10, since=EPOCH + timedelta(days=3))

    assert [row["changed_at"].day for row in pending] == [15]


@pytest.mark.integration
def test_a_created_memory_keeps_its_created_flag(store: PgDeltaStore) -> None:
    """A create has no search side; a reader must not treat it as an edit of nothing."""
    born = Delta(
        uri=A,
        memory_type="entities",
        field="content",
        search="",
        replace="# ov-clip\n\nA browser extension.",
        created=True,
        changed_at=EPOCH,
    )
    asyncio.run(store.record([born]))

    row = store.pending(limit=10)[0]

    assert row["created"] is True
    assert row["search"] == ""
    assert row["replace"].startswith("# ov-clip")


@pytest.mark.integration
def test_the_bootstrap_creates_a_usable_table(dsn: str, test_schema: str) -> None:
    """The path a real server takes: bootstrap with the flag, then write."""
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            for statement in ddl.bootstrap_statements(
                test_schema, create_extension=False, keep_deltas=True
            ):
                cur.execute(statement)
            conn.commit()

        written = PgDeltaStore(pool, test_schema)
        asyncio.run(written.record([delta()]))
        assert len(written.pending(limit=10)) == 1
    finally:
        pool.close()


@pytest.mark.integration
def test_running_the_bootstrap_twice_keeps_the_rows(dsn: str, test_schema: str) -> None:
    """Every server start runs it again; a delta table that resets is worthless."""
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True)
    try:

        def bootstrap() -> None:
            with pool.connection() as conn, conn.cursor() as cur:
                for statement in ddl.bootstrap_statements(
                    test_schema, create_extension=False, keep_deltas=True
                ):
                    cur.execute(statement)
                conn.commit()

        bootstrap()
        store = PgDeltaStore(pool, test_schema)
        asyncio.run(store.record([delta()]))
        bootstrap()

        assert len(store.pending(limit=10)) == 1
    finally:
        pool.close()
