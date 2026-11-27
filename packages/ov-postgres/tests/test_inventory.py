"""OpenViking's incremental inventory sees every row, timestamped or not.

``_timestamp_scan_pages`` walks ``updated_at`` ascending in pages. It walks the
leading pages by offset while they carry no timestamp, then bounds each later
page by the last timestamp seen -- so rows without one must sort first, or a
bounded page excludes them and the inventory never lists them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from .test_integration import DIM, vec
from .test_time_decay import _SharedVectorConfig

pytestmark = pytest.mark.integration

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_the_inventory_walk_lists_rows_without_a_timestamp(
    dsn: str, test_schema: str
) -> None:
    """Twenty-five timestamped rows and five without, in pages of ten: all thirty."""
    from openviking.server.identity import RequestContext, Role, UserIdentifier
    from openviking.storage.collection_schemas import CollectionSchemas
    from openviking.storage.expr import Eq
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    async def walk() -> list[str]:
        store = VikingVectorIndexBackend(
            VectorDBBackendConfig(
                backend="ov_postgres.adapter.PgVectorCollectionAdapter",
                name="context",
                index_name="default",
                dimension=DIM,
                distance_metric="cosine",
                custom_params={"dsn": dsn, "schema": test_schema},
            )
        )
        store.set_vector_config_resolver(_SharedVectorConfig())
        try:
            await store.create_collection(
                "context", CollectionSchemas.context_collection("context", DIM)
            )
            rows = [
                {
                    "id": f"t{index:02d}",
                    "uri": f"viking://resources/t{index:02d}.md",
                    "account_id": "acme",
                    "level": 2,
                    "vector": vec(index),
                    "updated_at": (START + timedelta(hours=index)).isoformat(),
                }
                for index in range(25)
            ]
            rows += [
                {
                    "id": f"n{index}",
                    "uri": f"viking://resources/n{index}.md",
                    "account_id": "acme",
                    "level": 2,
                    "vector": vec(100 + index),
                }
                for index in range(5)
            ]
            store._shared_adapter.upsert(rows)
            ctx = RequestContext(user=UserIdentifier("acme", "bob"), role=Role.ROOT)
            seen: list[str] = []
            async for page in store._timestamp_scan_pages(
                ctx, Eq("account_id", "acme"), batch_size=10, output_fields=["id"]
            ):
                seen.extend(str(row["id"]) for row in page)
            return seen
        finally:
            await store.close()

    seen = asyncio.run(walk())
    assert sorted(set(seen)) == sorted(
        [f"t{index:02d}" for index in range(25)] + [f"n{index}" for index in range(5)]
    )
