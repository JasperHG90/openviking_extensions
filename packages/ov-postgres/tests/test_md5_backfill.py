"""The md5 backfill gives every old row the md5 OpenViking itself computes.

The proof is OpenViking's own maintenance-reindex planner, ``resolve_rfv_state``,
run over a real ``VikingFS``: before the backfill it marks every reconciled row
stale -- the reindex would re-embed it -- and afterwards it marks every one
complete.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

from ov_postgres.adapter import PgVectorCollectionAdapter

from .test_integration import make_adapter
from .test_schema_reconcile import NEW, OLD, create, resource

pytestmark = pytest.mark.integration

ROOT_URI = "viking://resources/docs"
FILES = {
    "a.md": b"# Alpha\n\nThe first document.\n",
    "b.md": "# Béta\n\nUnicode bytes, hashed as stored.\n".encode(),
}
ABSTRACT = "Two documents about letters."
OVERVIEW = "## Files\n\n- a.md: alpha\n- b.md: beta\n"


@pytest.fixture
def viking_fs() -> Iterator[Any]:
    """A real RAGFS filesystem over a temporary directory."""
    from openviking.storage.viking_fs import VikingFS
    from openviking.utils.agfs_utils import RagfsBindingConfig, create_agfs_client
    from openviking_cli.utils.config.agfs_config import AGFSConfig

    with tempfile.TemporaryDirectory() as workspace:
        client = create_agfs_client(
            RagfsBindingConfig(agfs=AGFSConfig(backend="local", path=workspace))
        )
        yield VikingFS(agfs=client)


def root_ctx() -> Any:
    """The context the files are written under."""
    from openviking.server.identity import RequestContext, Role, UserIdentifier

    return RequestContext(user=UserIdentifier("acme", "default"), role=Role.ROOT)


async def write_tree(viking_fs: Any) -> None:
    """Write two files and the directory's abstract and overview sidecars."""
    ctx = root_ctx()
    for name, data in FILES.items():
        await viking_fs.write_file_bytes(f"{ROOT_URI}/{name}", data, ctx=ctx)
    await viking_fs.write_file(f"{ROOT_URI}/.abstract.md", ABSTRACT, ctx=ctx)
    await viking_fs.write_file(f"{ROOT_URI}/.overview.md", OVERVIEW, ctx=ctx)


def old_rows() -> list[dict[str, Any]]:
    """One row per level OpenViking indexes, written before md5 existed."""
    return [
        resource("a", uri=f"{ROOT_URI}/a.md"),
        resource("b", uri=f"{ROOT_URI}/b.md"),
        resource("gone", uri=f"{ROOT_URI}/gone.md"),
        resource("dir0", uri=ROOT_URI, level=0),
        resource("dir1", uri=ROOT_URI, level=1),
    ]


async def reindex_states(
    adapter: PgVectorCollectionAdapter, viking_fs: Any
) -> dict[str, str]:
    """Return what OpenViking's reindex planner decides for each node."""
    from openviking.storage.resource_rfv import build_rfv_snapshot, resolve_rfv_state
    from openviking.storage.resource_rnfv import RequestIntent

    intent = RequestIntent(target_uri=ROOT_URI, processing_mode="vectors_only")
    fields = sorted(intent.required_vector_fields() | {"abstract"})
    rows = adapter.query(
        filter={"op": "must", "field": "id", "conds": ["a", "b", "dir0", "dir1"]},
        output_fields=fields,
        limit=10,
    )
    snapshot = await build_rfv_snapshot(
        viking_fs=viking_fs,
        vikingdb=None,
        target_uri=ROOT_URI,
        ctx=root_ctx(),
        request_intent=intent,
        vector_inventory={str(row["id"]): row for row in rows},
    )
    diff = resolve_rfv_state(snapshot)
    return {path: str(entry.index_state.value) for path, entry in diff.entries.items()}


def test_after_the_backfill_openvikings_reindex_finds_nothing_stale(
    dsn: str, test_schema: str, viking_fs: Any
) -> None:
    """Before: every reconciled row is stale. After: every one is complete."""
    from openviking.utils.content_hash import content_md5

    asyncio.run(write_tree(viking_fs))
    old = make_adapter(dsn, test_schema)
    create(old, OLD)
    old.upsert(old_rows())
    old.close()

    adapter = make_adapter(dsn, test_schema)
    try:
        assert create(adapter, NEW) is False
        inner = adapter._pg_collection
        assert inner is not None and inner.md5_backfill_pending()
        before = asyncio.run(reindex_states(adapter, viking_fs))
        assert before == {"": "stale", "a.md": "stale", "b.md": "stale"}

        filled = asyncio.run(adapter.run_md5_backfill(viking_fs))

        assert filled == 4, "a, b and both directory levels; gone.md has no file"
        assert not inner.md5_backfill_pending()
        stored = {r["id"]: r["md5"] for r in adapter.get(["a", "b", "gone"])}
        assert stored == {
            "a": content_md5(FILES["a.md"]),
            "b": content_md5(FILES["b.md"]),
            "gone": "",
        }
        after = asyncio.run(reindex_states(adapter, viking_fs))
        assert after == {"": "complete", "a.md": "complete", "b.md": "complete"}
    finally:
        adapter.close()


def test_the_backfill_never_overwrites_an_md5_written_meanwhile(
    dsn: str, test_schema: str, viking_fs: Any
) -> None:
    """A row OpenViking rewrote after the reconcile keeps its own md5."""
    asyncio.run(write_tree(viking_fs))
    old = make_adapter(dsn, test_schema)
    create(old, OLD)
    old.upsert(old_rows())
    old.close()

    adapter = make_adapter(dsn, test_schema)
    try:
        create(adapter, NEW)
        adapter.upsert(resource("a", uri=f"{ROOT_URI}/a.md", md5="written-by-openviking"))
        asyncio.run(adapter.run_md5_backfill(viking_fs))
        assert adapter.get(["a"])[0]["md5"] == "written-by-openviking"
    finally:
        adapter.close()


def test_a_fresh_table_has_nothing_to_backfill(
    dsn: str, test_schema: str, viking_fs: Any
) -> None:
    """Only a reconcile that adds md5 marks the table; a new one is never marked."""
    adapter = make_adapter(dsn, test_schema)
    try:
        create(adapter, NEW)
        adapter.upsert(resource("a", uri=f"{ROOT_URI}/a.md"))
        assert asyncio.run(adapter.run_md5_backfill(viking_fs)) == 0
        assert adapter.get(["a"])[0]["md5"] == ""
    finally:
        adapter.close()
