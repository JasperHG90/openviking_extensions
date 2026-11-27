"""The md5 backfill gives every old row the md5 OpenViking itself computes.

The proof is OpenViking's own maintenance-reindex planner, ``resolve_rfv_state``,
run over a real ``VikingFS``: before the backfill it marks every reconciled row
stale -- the reindex would re-embed it -- and afterwards it marks every one
complete.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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
    """Yield a real RAGFS filesystem over a temporary directory."""
    from openviking.storage.viking_fs import VikingFS
    from openviking.utils.agfs_utils import RagfsBindingConfig, create_agfs_client
    from openviking_cli.utils.config.agfs_config import AGFSConfig

    with tempfile.TemporaryDirectory() as workspace:
        client = create_agfs_client(
            RagfsBindingConfig(agfs=AGFSConfig(backend="local", path=workspace))
        )
        yield VikingFS(agfs=client)


def root_ctx() -> Any:
    """Return the context the files are written under."""
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


def test_the_backfill_reads_files_an_acl_restricts(
    dsn: str, test_schema: str, viking_fs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ACL on, a restricted file is hashed too, as OpenViking's own scans read it."""
    import openviking.storage.viking_fs._access as access
    from openviking.utils.content_hash import content_md5

    asyncio.run(write_tree(viking_fs))
    old = make_adapter(dsn, test_schema)
    create(old, OLD)
    old.upsert(old_rows())
    old.close()

    class RestrictedEverywhere:
        """An ACL manager under which nobody holds a grant."""

        async def is_enabled(self, account_id: str) -> bool:
            return True

        async def resolve_many(self, uris: Any, ctx: Any) -> dict[str, Any]:
            return {uri: object() for uri in uris}

    viking_fs.acl_manager = RestrictedEverywhere()
    monkeypatch.setattr(access, "acl_permission", lambda effective, ctx: None)

    adapter = make_adapter(dsn, test_schema)
    try:
        create(adapter, NEW)
        asyncio.run(adapter.run_md5_backfill(viking_fs))
        assert adapter.get(["a"])[0]["md5"] == content_md5(FILES["a.md"])
    finally:
        adapter.close()


def test_record_md5_keeps_an_md5_written_meanwhile(dsn: str, test_schema: str) -> None:
    """The guard on the write itself: a row given an md5 since the read keeps it."""
    adapter = make_adapter(dsn, test_schema)
    try:
        create(adapter, NEW)
        adapter.upsert(resource("a", uri=f"{ROOT_URI}/a.md", md5="written-by-openviking"))
        inner = adapter._pg_collection
        assert inner is not None
        assert inner.record_md5("a", "from-the-backfill") is False
        assert adapter.get(["a"])[0]["md5"] == "written-by-openviking"
    finally:
        adapter.close()


class CountingReader:
    """A VikingFS stand-in that counts reads and can act on the first one."""

    def __init__(self, inner: Any, on_first: Any = None, delay: float = 0.0) -> None:
        self.inner = inner
        self.reads = 0
        self.on_first = on_first
        self.delay = delay

    async def read_file_bytes(self, uri: str, ctx: object = None) -> bytes:
        """Count the read, run the hook on the first, then read for real."""
        self.reads += 1
        if self.reads == 1 and self.on_first is not None:
            await asyncio.to_thread(self.on_first)
        if self.delay:
            await asyncio.sleep(self.delay)
        data: bytes = await self.inner.read_file_bytes(uri, ctx=ctx)
        return data


def reconciled_old_table(dsn: str, schema: str, viking_fs: Any) -> None:
    """Old rows, the files they index, and a reconcile that marks the backfill."""
    asyncio.run(write_tree(viking_fs))
    old = make_adapter(dsn, schema)
    create(old, OLD)
    old.upsert(old_rows())
    old.close()
    reconciling = make_adapter(dsn, schema)
    create(reconciling, NEW)
    reconciling.close()


def test_only_one_process_backfills_at_a_time(
    dsn: str, test_schema: str, viking_fs: Any
) -> None:
    """Several workers starting together share one run, not one each."""
    reconciled_old_table(dsn, test_schema, viking_fs)
    adapters = [make_adapter(dsn, test_schema) for _ in range(3)]
    reader = CountingReader(viking_fs, delay=0.2)

    async def all_at_once() -> list[int]:
        return list(await asyncio.gather(*(a.run_md5_backfill(reader) for a in adapters)))

    try:
        for adapter in adapters:
            assert adapter.collection_exists()
        filled = asyncio.run(all_at_once())
        assert sorted(filled) == [0, 0, 4]
        assert reader.reads == 5, "each row read once: a, b, gone and two sidecars"
    finally:
        for adapter in adapters:
            adapter.close()


def test_a_backfill_queued_again_during_a_run_stays_queued(
    dsn: str, test_schema: str, viking_fs: Any
) -> None:
    """A re-queue from another server mid-run is not cleared by the run ending."""
    reconciled_old_table(dsn, test_schema, viking_fs)
    adapter = make_adapter(dsn, test_schema)
    try:
        assert adapter.collection_exists()
        inner = adapter._pg_collection
        assert inner is not None
        reader = CountingReader(viking_fs, on_first=inner._queue_md5_backfill)
        asyncio.run(adapter.run_md5_backfill(reader))
        assert inner.md5_backfill_pending(), "queued during the run: still pending"
        asyncio.run(adapter.run_md5_backfill(viking_fs))
        assert not inner.md5_backfill_pending()
    finally:
        adapter.close()


def test_a_lease_left_by_a_crashed_process_expires(
    dsn: str, test_schema: str, viking_fs: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A run that died holding the lease blocks others only until it lapses."""
    reconciled_old_table(dsn, test_schema, viking_fs)
    adapter = make_adapter(dsn, test_schema)
    try:
        assert adapter.collection_exists()
        inner = adapter._pg_collection
        assert inner is not None
        assert inner.take_md5_lease("crashed-worker", 1.0)
        with caplog.at_level(logging.INFO, logger="ov_postgres.md5_backfill"):
            assert asyncio.run(adapter.run_md5_backfill(viking_fs)) == 0
        assert any(
            r.levelno == logging.INFO and "holds the lease" in r.getMessage()
            for r in caplog.records
        )
        time.sleep(1.5)
        assert asyncio.run(adapter.run_md5_backfill(viking_fs)) == 4
        assert not inner.md5_backfill_pending()
    finally:
        adapter.close()


def test_of_racing_takers_exactly_one_gets_the_lease(
    dsn: str, test_schema: str, viking_fs: Any
) -> None:
    """The lease is taken by a conditional update, so a race has one winner."""
    reconciled_old_table(dsn, test_schema, viking_fs)
    adapters = [make_adapter(dsn, test_schema) for _ in range(8)]
    try:
        inners = []
        for adapter in adapters:
            assert adapter.collection_exists()
            inners.append(adapter._pg_collection)
        with ThreadPoolExecutor(max_workers=8) as pool:
            won = list(
                pool.map(
                    lambda pair: pair[1].take_md5_lease(f"worker-{pair[0]}", 60.0),
                    enumerate(inners),
                )
            )
        assert won.count(True) == 1
    finally:
        for adapter in adapters:
            adapter.close()


def test_a_run_that_loses_its_lease_stops_without_clearing_the_mark(
    dsn: str, test_schema: str, viking_fs: Any
) -> None:
    """If another process took over, this run must not finish its work for it."""
    reconciled_old_table(dsn, test_schema, viking_fs)
    adapter = make_adapter(dsn, test_schema)
    try:
        assert adapter.collection_exists()
        inner = adapter._pg_collection
        assert inner is not None

        def steal() -> None:
            inner.release_md5_lease(None)
            assert inner.take_md5_lease("someone-else", 60.0)

        reader = CountingReader(viking_fs, on_first=steal)
        asyncio.run(adapter.run_md5_backfill(reader, batch_size=1))
        assert inner.md5_backfill_pending()
    finally:
        adapter.close()
