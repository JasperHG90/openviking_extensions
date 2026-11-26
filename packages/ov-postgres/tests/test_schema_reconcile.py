"""A table created by an older OpenViking gains the fields added since.

OpenViking 0.4.18 to 0.5 added ``md5`` and three ACL fields to the context
collection. It migrates an existing collection only for its ``local`` and
``cuvs`` backends, so this backend reconciles the table itself when
``create_collection`` finds it already there.
"""

from __future__ import annotations

import copy
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


def test_a_value_the_column_cannot_hold_stays_in_extra(
    dsn: str, test_schema: str
) -> None:
    """A stashed value of the wrong type is kept rather than failing the boot."""
    old = make_adapter(dsn, test_schema)
    create(old, OLD)
    old.upsert(resource("odd", acl_direct_grants={"not": "a list"}))
    old.close()

    adapter = reopen(dsn, test_schema, NEW)
    try:
        record = adapter.get(["odd"])[0]
        assert record["acl_direct_grants"] == [EMPTY_ACL_GRANT]
        with psycopg.connect(dsn) as conn:
            extra = conn.execute(
                f'SELECT extra FROM "{test_schema}".ov_context WHERE id = %s',
                ("odd",),
            ).fetchone()
        assert extra is not None and extra[0]["acl_direct_grants"] == {"not": "a list"}
    finally:
        adapter.close()


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
