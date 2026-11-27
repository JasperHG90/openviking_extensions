"""A table created by an older OpenViking gains the fields added since.

OpenViking 0.4.18 to 0.5 added ``md5`` and three ACL fields to the context
collection. It migrates an existing collection only for its ``local`` and
``cuvs`` backends, so this backend reconciles the table itself when
``create_collection`` finds it already there.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from openviking.server.identity import RequestContext, Role, UserIdentifier
from openviking.storage.acl import (
    ACL_CONTEXT_FIELDS,
    EMPTY_ACL_GRANT,
    AclAction,
    acl_grant_tokens,
)
from openviking.storage.collection_schemas import CollectionSchemas
from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

from ov_postgres.adapter import PgVectorCollectionAdapter
from ov_postgres.collection import PgVectorCollection

from .test_integration import DIM, make_adapter, vec

pytestmark = pytest.mark.integration

NEW_FIELDS = {"md5", *ACL_CONTEXT_FIELDS}
NEW: dict[str, Any] = CollectionSchemas.context_collection("context", DIM)
OLD: dict[str, Any] = copy.deepcopy(NEW)
OLD["Fields"] = [f for f in OLD["Fields"] if f["FieldName"] not in NEW_FIELDS]
OLD["ScalarIndex"] = [f for f in OLD["ScalarIndex"] if f not in ACL_CONTEXT_FIELDS]

BOB = RequestContext(user=UserIdentifier("acme", "bob"), role=Role.USER)


def create(adapter: PgVectorCollectionAdapter, meta: dict[str, Any]) -> bool:
    """Call ``create_collection`` the way OpenViking does on every boot."""
    return adapter.create_collection(
        "context", meta, distance="cosine", sparse_weight=0.0, index_name="default"
    )


def columns(dsn: str, schema: str) -> set[str]:
    """Return the column names of the context table."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'ov_context'",
            (schema,),
        ).fetchall()
    return {str(row[0]) for row in rows}


def indexed_columns(dsn: str, schema: str) -> str:
    """Return every index definition on the context table, joined."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = 'ov_context'",
            (schema,),
        ).fetchall()
    return "\n".join(str(row[0]) for row in rows)


def resource(record_id: str, **fields: Any) -> dict[str, Any]:
    """Build a resource record in the ``acme`` account."""
    return {
        "id": record_id,
        "uri": f"viking://resources/team/{record_id}.md",
        "account_id": "acme",
        "level": 2,
        "context_type": "resource",
        "vector": vec(len(record_id)),
        **fields,
    }


def reopen(dsn: str, schema: str, meta: dict[str, Any]) -> PgVectorCollectionAdapter:
    """Open the existing collection as a fresh server would, then boot it."""
    adapter = make_adapter(dsn, schema)
    assert create(adapter, meta) is False
    return adapter


def test_an_old_table_gains_the_new_columns_indexes_and_values(
    dsn: str, test_schema: str
) -> None:
    """Columns, scalar indexes, defaults and stashed values all land."""
    old = make_adapter(dsn, test_schema)
    assert create(old, OLD) is True
    # Written while the table lacked the columns, so both values went to
    # `extra` -- what a 0.4.18+ OpenViking writes to a 0.4.17-era table.
    old.upsert(resource("stashed", md5="abc123", acl_mode="restricted"))
    old.upsert(resource("plain"))
    old.close()

    adapter = reopen(dsn, test_schema, NEW)
    try:
        assert NEW_FIELDS <= columns(dsn, test_schema)
        definitions = indexed_columns(dsn, test_schema)
        for field in ACL_CONTEXT_FIELDS:
            assert f"({field})" in definitions, field

        rows = {
            r["id"]: r
            for r in adapter.query(
                filter={"op": "must", "field": "account_id", "conds": ["acme"]},
                output_fields=["id", "md5", "acl_mode", "acl_direct_grants"],
                limit=10,
            )
        }
        assert rows["stashed"]["md5"] == "abc123"
        assert rows["stashed"]["acl_mode"] == "restricted"
        assert rows["plain"]["md5"] == ""
        assert rows["plain"]["acl_mode"] == "none"
        assert rows["plain"]["acl_direct_grants"] == [EMPTY_ACL_GRANT]

        # Moved, not copied: `extra` no longer carries the old values.
        record = adapter.get(["stashed"])[0]
        assert record["md5"] == "abc123"
        with psycopg.connect(dsn) as conn:
            extra = conn.execute(
                f'SELECT extra FROM "{test_schema}".ov_context WHERE id = %s',
                ("stashed",),
            ).fetchone()
        assert extra is not None and not NEW_FIELDS & set(extra[0])

        meta = adapter.get_collection().get_meta_data()
        assert NEW_FIELDS <= {f["FieldName"] for f in meta["Fields"]}
        assert ACL_CONTEXT_FIELDS <= set(meta["ScalarIndex"])
    finally:
        adapter.close()


def test_a_second_boot_changes_nothing(dsn: str, test_schema: str) -> None:
    """Reconciling an up-to-date table is a no-op."""
    old = make_adapter(dsn, test_schema)
    create(old, OLD)
    old.close()
    reopen(dsn, test_schema, NEW).close()

    adapter = make_adapter(dsn, test_schema)
    try:
        assert adapter.collection_exists()
        inner = adapter._pg_collection
        assert inner is not None
        assert inner.reconcile_schema(NEW) is None
        assert create(adapter, NEW) is False
    finally:
        adapter.close()


def test_a_field_the_schema_drops_keeps_its_column(dsn: str, test_schema: str) -> None:
    """Reconciling only adds: an older, narrower schema removes nothing."""
    adapter = make_adapter(dsn, test_schema)
    create(adapter, NEW)
    adapter.close()
    reopen(dsn, test_schema, OLD).close()
    assert NEW_FIELDS <= columns(dsn, test_schema)


@pytest.mark.parametrize("value", [{"not": "a string"}, 7])
def test_a_value_the_column_cannot_hold_stays_in_extra(
    dsn: str, test_schema: str, value: object
) -> None:
    """A stashed non-ACL value of the wrong type is kept rather than failing the boot.

    ``md5`` only speeds up the incremental diff, so a value that does not fit
    costs nothing to leave where it is. The column takes its default.
    """
    old = make_adapter(dsn, test_schema)
    create(old, OLD)
    old.upsert(resource("odd", md5=value))
    old.close()

    adapter = reopen(dsn, test_schema, NEW)
    try:
        assert adapter.get(["odd"])[0]["md5"] == ""
        with psycopg.connect(dsn) as conn:
            extra = conn.execute(
                f'SELECT extra FROM "{test_schema}".ov_context WHERE id = %s',
                ("odd",),
            ).fetchone()
        assert extra is not None and extra[0]["md5"] == value
    finally:
        adapter.close()


def restricted_and_open_on_an_old_table(dsn: str, schema: str) -> None:
    """Write a restricted and an open resource while the table lacks ACL columns."""
    old = make_adapter(dsn, schema)
    create(old, OLD)
    grants = acl_grant_tokens(["user:alice"], AclAction.READ)
    old.upsert(resource("secret", acl_mode="restricted", acl_direct_grants=grants[:1]))
    old.upsert(resource("open"))
    old.close()


def visible_to_bob(adapter: PgVectorCollectionAdapter) -> set[str]:
    """Return the ids an ACL-scoped query shows a user with no grants."""
    tenant = VikingVectorIndexBackend._tenant_filter(None, BOB, acl_enabled=True)
    return {r["id"] for r in adapter.query(filter=tenant, limit=10, output_fields=["id"])}


def test_a_failed_reconcile_fails_closed_until_one_succeeds(
    dsn: str, test_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconcile that fails must not leave the server serving an ACL-less table.

    OpenViking's ``create_collection`` catches the error, logs it and boots
    anyway. Without the ACL columns every row reads as unrestricted, so the
    adapter refuses every query until a reconcile succeeds -- here, the retry
    the first query after the cause clears makes.
    """
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    restricted_and_open_on_an_old_table(dsn, test_schema)

    def denied(self: object, cur: object, spec: object) -> None:
        raise psycopg.errors.InsufficientPrivilege("must be owner of table ov_context")

    original = PgVectorCollection._add_column
    monkeypatch.setattr(PgVectorCollection, "_add_column", denied)
    monkeypatch.setattr(PgVectorCollectionAdapter, "_RECONCILE_RETRY_SECONDS", 0.0)
    backend = VikingVectorIndexBackend(
        VectorDBBackendConfig(
            backend="ov_postgres.adapter.PgVectorCollectionAdapter",
            name="context",
            index_name="default",
            dimension=DIM,
            distance_metric="cosine",
            custom_params={"dsn": dsn, "schema": test_schema},
        )
    )

    async def boot_and_query() -> None:
        try:
            assert await backend.create_collection("context", NEW) is False
            adapter = backend._shared_adapter
            with pytest.raises(RuntimeError, match="reconcile"):
                visible_to_bob(adapter)
            with pytest.raises(RuntimeError, match="reconcile"):
                adapter.upsert(resource("late"))

            monkeypatch.setattr(PgVectorCollection, "_add_column", original)
            assert visible_to_bob(adapter) == {"open"}
        finally:
            await backend.close()

    asyncio.run(boot_and_query())


def test_an_acl_value_that_cannot_be_moved_fails_the_reconcile(
    dsn: str, test_schema: str
) -> None:
    """A stashed ACL value that does not fit is never replaced by "unrestricted".

    Leaving it in ``extra`` would give the row the column default, ``none``, and
    show a resource someone restricted to everybody. The reconcile fails
    instead, and the adapter refuses queries until the row is repaired.
    """
    old = make_adapter(dsn, test_schema)
    create(old, OLD)
    old.upsert(resource("odd", acl_mode=["not", "a string"]))
    old.close()

    adapter = make_adapter(dsn, test_schema)
    try:
        with pytest.raises(ValueError, match="acl_mode"):
            create(adapter, NEW)
        with pytest.raises(RuntimeError, match="reconcile"):
            adapter.query(limit=10)
        assert "acl_mode" not in columns(dsn, test_schema)
    finally:
        adapter.close()


def test_two_servers_reconciling_at_once_serialise_on_the_lock(
    dsn: str, test_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent boots take turns; unserialised, they race to build one index.

    The table already has every column and lacks only the ACL scalar indexes,
    so neither reconcile takes the ``ACCESS EXCLUSIVE`` lock an ``ALTER TABLE``
    would. Both are held at the index step together for up to two seconds: with
    the advisory lock the second never gets there until the first commits, and
    without it both see the indexes missing and both build them, and
    PostgreSQL refuses the second ``CREATE INDEX``.
    """
    indexes_only = copy.deepcopy(NEW)
    indexes_only["ScalarIndex"] = [
        f for f in NEW["ScalarIndex"] if f not in ACL_CONTEXT_FIELDS
    ]
    first = make_adapter(dsn, test_schema)
    create(first, indexes_only)
    first.close()

    together = threading.Barrier(2)
    build = PgVectorCollection._create_new_indexes

    def held_at_the_index_step(self: Any, cur: Any, **schemas: Any) -> list[str]:
        try:
            together.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        built: list[str] = build(self, cur, **schemas)
        return built

    monkeypatch.setattr(PgVectorCollection, "_create_new_indexes", held_at_the_index_step)
    adapters = [make_adapter(dsn, test_schema) for _ in range(2)]
    try:
        for adapter in adapters:
            assert adapter.collection_exists()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda a: create(a, NEW), adapters))
        assert results == [False, False]
        meta = adapters[0].get_collection().get_meta_data()
        names = [f["FieldName"] for f in meta["Fields"]]
        assert len(names) == len(set(names))
        assert len(meta["ScalarIndex"]) == len(set(meta["ScalarIndex"]))
        assert ACL_CONTEXT_FIELDS <= set(meta["ScalarIndex"])
    finally:
        for adapter in adapters:
            adapter.close()


def test_a_rebuild_that_fails_after_the_commit_still_fails_closed(
    dsn: str, test_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view must be rebuilt before the reconcile counts as done.

    The reconcile's transaction commits, then rebuilding the adapter's view
    queries the database again. If that query fails, the old view -- with no
    ACL fields -- must not go on serving as though nothing were pending.
    """
    restricted_and_open_on_an_old_table(dsn, test_schema)
    monkeypatch.setattr(PgVectorCollectionAdapter, "_RECONCILE_RETRY_SECONDS", 0.0)
    adapter = make_adapter(dsn, test_schema)
    try:
        assert adapter.collection_exists()
        lookup = PgVectorCollectionAdapter._stored_index_distance
        failures = iter([psycopg.OperationalError("connection dropped")])

        def flaky(self: PgVectorCollectionAdapter) -> str | None:
            failure = next(failures, None)
            if failure is not None:
                raise failure
            return lookup(self)

        monkeypatch.setattr(PgVectorCollectionAdapter, "_stored_index_distance", flaky)
        with pytest.raises(psycopg.OperationalError):
            create(adapter, NEW)
        assert visible_to_bob(adapter) == {"open"}, "the retry must rebuild the view"
    finally:
        adapter.close()


def test_a_pending_reconcile_is_retried_at_most_once_per_interval(
    dsn: str, test_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between retries, requests are refused at once rather than queued.

    Each attempt can wait out the lock timeout under the adapter's lock, so
    retrying on every request would stall them all behind one another.
    """
    restricted_and_open_on_an_old_table(dsn, test_schema)
    attempts: list[int] = []
    reconcile = PgVectorCollection.reconcile_schema

    def counted(self: PgVectorCollection, meta: dict[str, Any]) -> Any:
        attempts.append(1)
        return reconcile(self, meta)

    def denied(self: object, cur: object, spec: object) -> None:
        raise psycopg.errors.InsufficientPrivilege("must be owner of table ov_context")

    monkeypatch.setattr(PgVectorCollection, "reconcile_schema", counted)
    monkeypatch.setattr(PgVectorCollection, "_add_column", denied)
    monkeypatch.setattr(PgVectorCollectionAdapter, "_RECONCILE_RETRY_SECONDS", 3600.0)
    adapter = make_adapter(dsn, test_schema)
    try:
        assert adapter.collection_exists()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            create(adapter, NEW)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="reconcile"):
                adapter.query(limit=10)
        assert len(attempts) == 1, "no retry before the interval has passed"

        monkeypatch.setattr(PgVectorCollectionAdapter, "_RECONCILE_RETRY_SECONDS", 0.0)
        with pytest.raises(RuntimeError, match="reconcile"):
            adapter.query(limit=10)
        assert len(attempts) == 2, "a retry once the interval has passed"
    finally:
        adapter.close()


def test_a_failed_reconcile_stops_openviking_from_booting(
    dsn: str, test_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenViking's own boot path reads the collection meta right after creating it.

    ``init_context_collection`` calls ``create_collection``, which swallows
    the error, and then ``get_collection_meta``, which does not -- so a
    reconcile that fails at boot stops the server starting.
    """
    from openviking.storage.collection_schemas import init_context_collection
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    restricted_and_open_on_an_old_table(dsn, test_schema)

    def denied(self: object, cur: object, spec: object) -> None:
        raise psycopg.errors.InsufficientPrivilege("must be owner of table ov_context")

    monkeypatch.setattr(PgVectorCollection, "_add_column", denied)
    backend = VikingVectorIndexBackend(
        VectorDBBackendConfig(
            backend="ov_postgres.adapter.PgVectorCollectionAdapter",
            name="context",
            index_name="default",
            dimension=DIM,
            distance_metric="cosine",
            custom_params={"dsn": dsn, "schema": test_schema},
        )
    )

    async def boot() -> None:
        try:
            with pytest.raises(RuntimeError, match="reconcile"):
                await init_context_collection(backend)
        finally:
            await backend.close()

    asyncio.run(boot())


@pytest.mark.parametrize("meta", [OLD, NEW], ids=["table-from-0.4", "fresh-table"])
def test_acl_filter_hides_a_restricted_resource(
    dsn: str, test_schema: str, meta: dict[str, Any]
) -> None:
    """With ACL on, a restricted resource stays hidden from a user without a grant.

    On a table from before the ACL fields this used to let every restricted
    resource through: the filter compiler read the undeclared ``acl_mode`` as
    NULL, so ``must_not acl_mode in [inherit, restricted]`` held for every row.
    """
    first = make_adapter(dsn, test_schema)
    create(first, meta)
    first.close()

    adapter = reopen(dsn, test_schema, NEW)
    try:
        grants = acl_grant_tokens(["user:alice"], AclAction.READ)
        adapter.upsert(
            resource(
                "secret",
                acl_mode="restricted",
                acl_direct_grants=grants[:1],
                acl_inherited_grants=[EMPTY_ACL_GRANT],
            )
        )
        adapter.upsert(resource("open"))
        tenant = VikingVectorIndexBackend._tenant_filter(None, BOB, acl_enabled=True)
        seen = {
            r["uri"]
            for r in adapter.query(filter=tenant, limit=10, output_fields=["uri"])
        }
        assert seen == {"viking://resources/team/open.md"}
    finally:
        adapter.close()


def test_a_server_that_loaded_before_another_reconciled_sees_the_new_fields(
    dsn: str, test_schema: str
) -> None:
    """Two servers boot together; the one that did not reconcile still catches up.

    B loads the old schema, A reconciles, then B's own reconcile finds the
    table current. B must rebuild its view anyway, or its writes put the ACL
    fields in ``extra`` and a restricted resource becomes visible to everyone.
    """
    first = make_adapter(dsn, test_schema)
    create(first, OLD)
    first.close()

    late = make_adapter(dsn, test_schema)
    try:
        assert late.collection_exists()
        reopen(dsn, test_schema, NEW).close()
        assert create(late, NEW) is False

        grants = acl_grant_tokens(["user:alice"], AclAction.READ)
        late.upsert(
            resource("secret", acl_mode="restricted", acl_direct_grants=grants[:1])
        )
        late.upsert(resource("open"))
        tenant = VikingVectorIndexBackend._tenant_filter(None, BOB, acl_enabled=True)
        seen = {
            r["uri"] for r in late.query(filter=tenant, limit=10, output_fields=["uri"])
        }
        assert seen == {"viking://resources/team/open.md"}
    finally:
        late.close()


def test_requests_arriving_together_share_one_retry(
    dsn: str, test_schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the retry window opens, only one of the waiting requests retries.

    The other finds, under the adapter's lock, that an attempt has just been
    made, and is refused instead of running a second one.
    """
    restricted_and_open_on_an_old_table(dsn, test_schema)
    attempts: list[int] = []
    reconcile = PgVectorCollection.reconcile_schema

    def slow(self: PgVectorCollection, meta: dict[str, Any]) -> Any:
        attempts.append(1)
        time.sleep(0.3)
        return reconcile(self, meta)

    def denied(self: object, cur: object, spec: object) -> None:
        raise psycopg.errors.InsufficientPrivilege("must be owner of table ov_context")

    monkeypatch.setattr(PgVectorCollection, "_add_column", denied)
    monkeypatch.setattr(PgVectorCollectionAdapter, "_RECONCILE_RETRY_SECONDS", 1.0)
    adapter = make_adapter(dsn, test_schema)
    try:
        assert adapter.collection_exists()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            create(adapter, NEW)
        monkeypatch.setattr(PgVectorCollection, "reconcile_schema", slow)
        adapter._last_reconcile_attempt = time.monotonic() - 2.0

        together = threading.Barrier(2)

        def query() -> None:
            together.wait(timeout=5)
            with pytest.raises(RuntimeError, match="reconcile"):
                adapter.query(limit=10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(query), pool.submit(query)]:
                future.result()
        assert len(attempts) == 1
    finally:
        adapter.close()


def stale_and_current(dsn: str, schema: str) -> tuple[Any, Any]:
    """Return a writer that loaded the old schema, and a reader that reconciled.

    The stale one stands for a server that started before another reconciled
    the table, including an older ov-postgres: it writes the new fields into
    ``extra`` because its view of the table has no columns for them.
    """
    first = make_adapter(dsn, schema)
    create(first, OLD)
    first.close()
    stale = make_adapter(dsn, schema)
    assert stale.collection_exists()
    current = reopen(dsn, schema, NEW)
    return stale, current


def extra_of(dsn: str, schema: str, key: str) -> dict[str, Any]:
    """Return a row's ``extra`` as stored."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            f'SELECT extra FROM "{schema}".ov_context WHERE id = %s', (key,)
        ).fetchone()
    assert row is not None
    return dict(row[0])


def test_an_old_style_write_after_the_reconcile_lands_in_its_columns(
    dsn: str, test_schema: str
) -> None:
    """A field written to ``extra`` by a stale writer is read, filtered and ACL'd right.

    Restricted by a writer that has no column for ``acl_mode``, the resource
    must still be hidden from a user without a grant.
    """
    stale, current = stale_and_current(dsn, test_schema)
    try:
        grants = acl_grant_tokens(["user:alice"], AclAction.READ)
        stale.upsert(
            resource(
                "secret", acl_mode="restricted", acl_direct_grants=grants[:1], md5="m1"
            )
        )
        stale.upsert(resource("plain"))

        assert visible_to_bob(current) == {"plain"}
        row = current.query(
            filter={"op": "must", "field": "id", "conds": ["secret"]},
            output_fields=["id", "acl_mode", "acl_direct_grants", "md5"],
            limit=1,
        )[0]
        assert row["acl_mode"] == "restricted"
        assert row["acl_direct_grants"] == grants[:1]
        assert row["md5"] == "m1"
        assert {
            r["id"]
            for r in current.query(
                filter={"op": "must", "field": "md5", "conds": ["m1"]}, limit=10
            )
        } == {"secret"}
        assert not NEW_FIELDS & set(extra_of(dsn, test_schema, "secret"))
        plain = current.get(["plain"])[0]
        assert plain["acl_mode"] == "none"
        assert plain["acl_direct_grants"] == [EMPTY_ACL_GRANT]
    finally:
        stale.close()
        current.close()


def test_a_raw_update_into_extra_is_moved_too(dsn: str, test_schema: str) -> None:
    """Whatever writes ``extra``, the value ends up in the column."""
    stale, current = stale_and_current(dsn, test_schema)
    try:
        current.upsert(resource("doc"))
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                f'UPDATE "{test_schema}".ov_context '
                "SET extra = extra || '{\"acl_mode\": \"restricted\"}' WHERE id = 'doc'"
            )
        assert current.get(["doc"])[0]["acl_mode"] == "restricted"
        assert visible_to_bob(current) == set()
    finally:
        stale.close()
        current.close()


def test_an_acl_value_a_stale_writer_cannot_store_is_refused(
    dsn: str, test_schema: str
) -> None:
    """An ACL value that fits no column fails the write rather than reading as open."""
    stale, current = stale_and_current(dsn, test_schema)
    try:
        with pytest.raises(psycopg.Error, match="acl_mode"):
            stale.upsert(resource("odd", acl_mode={"not": "a string"}))
        stale.upsert(resource("odd2", md5={"not": "a string"}))
        assert extra_of(dsn, test_schema, "odd2")["md5"] == {"not": "a string"}
        assert current.get(["odd2"])[0]["md5"] == ""
    finally:
        stale.close()
        current.close()


def test_a_writer_that_knows_nothing_of_acl_cannot_erase_a_restriction(
    dsn: str, test_schema: str
) -> None:
    """A whole-row rewrite by an ACL-unaware writer keeps the row's ACL and md5.

    OpenViking keeps ACL on the vector rows themselves. An older ov-postgres on
    an OpenViking without ACL rewrites a row with the defaults -- unrestricted
    -- and the restriction would be gone for good. This version's own writes
    still change ACL as asked.
    """
    adapter = make_adapter(dsn, test_schema)
    try:
        create(adapter, OLD)
        adapter.close()
        adapter = reopen(dsn, test_schema, NEW)
        grants = acl_grant_tokens(["user:alice"], AclAction.READ)
        adapter.upsert(
            resource("doc", acl_mode="restricted", acl_direct_grants=grants[:1], md5="m1")
        )
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                f'UPDATE "{test_schema}".ov_context SET acl_mode = %s, '
                "acl_direct_grants = %s, md5 = '' WHERE id = 'doc'",
                ("none", [EMPTY_ACL_GRANT]),
            )
        kept = adapter.get(["doc"])[0]
        assert (kept["acl_mode"], kept["acl_direct_grants"], kept["md5"]) == (
            "restricted",
            grants[:1],
            "m1",
        )

        adapter.upsert(resource("doc", acl_mode="none", md5="m2"))
        lifted = adapter.get(["doc"])[0]
        assert (lifted["acl_mode"], lifted["md5"]) == ("none", "m2")
    finally:
        adapter.close()
