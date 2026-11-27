"""Event time decay on find and search, as OpenViking's other backends apply it.

OpenViking passes an ``events_time_decay_protection`` request to the adapter as
``advance={"time_decay": {"protection": ..., "origin": ...}}``. Each score is
multiplied by OpenViking's fixed curve over the row's ``updated_at``; a row
with no timestamp keeps its score.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ov_postgres.adapter import PgVectorCollectionAdapter

from .test_integration import DIM, adapter  # noqa: F401  # the fixture

pytestmark = pytest.mark.integration

ORIGIN = datetime(2026, 6, 1, tzinfo=timezone.utc)
QUERY = [1.0] + [0.0] * (DIM - 1)
NEAREST = [1.0] + [0.0] * (DIM - 1)
NEAR = [0.9, 0.3] + [0.0] * (DIM - 2)
FAR = [0.3, 0.9] + [0.0] * (DIM - 2)


def decay(adapter: PgVectorCollectionAdapter, *, protection: str = "0", **kw: object) -> list[dict]:
    """Run a decayed vector query through the adapter, as OpenViking does."""
    return adapter.query(
        query_vector=QUERY,
        limit=10,
        advance={"time_decay": {"protection": protection, "origin": ORIGIN.isoformat()}},
        **kw,
    )


def expected_factor(updated: datetime, protection: str = "0") -> float:
    """OpenViking's own curve for this row."""
    from openviking.storage.vectordb_adapters.opengauss.retrieval import time_decay_factor
    from openviking.utils.time_decay import build_time_decay_fusion_spec

    spec = build_time_decay_fusion_spec(protection=protection, origin=ORIGIN)
    return float(time_decay_factor(updated.timestamp() * 1000, spec))


def test_a_recent_row_overtakes_an_older_closer_one(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Scores are the similarity times OpenViking's decay factor."""
    stale_at = ORIGIN - timedelta(days=60)
    fresh_at = ORIGIN - timedelta(hours=1)
    adapter.upsert(
        [
            {"id": "stale", "vector": NEAREST, "updated_at": stale_at.isoformat()},
            {"id": "fresh", "vector": NEAR, "updated_at": fresh_at.isoformat()},
            {"id": "undated", "vector": FAR},
        ]
    )
    plain = adapter.query(query_vector=QUERY, limit=10)
    assert plain[0]["id"] == "stale"

    rows = {row["id"]: row for row in decay(adapter)}
    assert [row["id"] for row in decay(adapter)][0] == "fresh"
    for key, when in (("stale", stale_at), ("fresh", fresh_at)):
        row = rows[key]
        assert row["_time_score"] == pytest.approx(expected_factor(when))
        assert row["_score"] == pytest.approx(row["_origin_score"] * row["_time_score"])
    assert rows["undated"]["_score"] == pytest.approx(rows["undated"]["_origin_score"])
    assert "_time_score" not in rows["undated"]


def test_the_protection_period_keeps_recent_rows_undecayed(
    adapter: PgVectorCollectionAdapter,
) -> None:
    """Within the protection window the factor is 1."""
    when = ORIGIN - timedelta(days=3)
    adapter.upsert({"id": "a", "vector": NEAR, "updated_at": when.isoformat()})
    row = decay(adapter, protection="7d")[0]
    assert row["_time_score"] == pytest.approx(1.0)
    assert expected_factor(when, "7d") == pytest.approx(1.0)


def test_decay_honours_filters_and_paging(adapter: PgVectorCollectionAdapter) -> None:
    """The decayed ranking is filtered first and paged after."""
    adapter.upsert(
        [
            {
                "id": f"d{index}",
                "vector": NEAR,
                "level": index % 2,
                "updated_at": (ORIGIN - timedelta(days=index)).isoformat(),
            }
            for index in range(6)
        ]
    )
    odd = decay(adapter, filter={"op": "must", "field": "level", "conds": [1]})
    assert [row["id"] for row in odd] == ["d1", "d3", "d5"]
    page = adapter.query(
        query_vector=QUERY,
        limit=2,
        offset=1,
        advance={"time_decay": {"protection": "0", "origin": ORIGIN.isoformat()}},
    )
    assert [row["id"] for row in page] == ["d1", "d2"]


class _SharedVectorConfig:
    """Resolve every account to the shared collection."""

    async def resolve(self, account_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            dedicated_vectordb=False, vectordb=SimpleNamespace(dimension=DIM)
        )


def test_openvikings_own_decayed_search_runs_on_this_backend(
    dsn: str, test_schema: str
) -> None:
    """Through ``search_in_tenant``, as ``find`` reaches it: events decay, nothing raises."""
    import asyncio

    from openviking.server.identity import RequestContext, Role, UserIdentifier
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    from openviking.storage.collection_schemas import CollectionSchemas

    async def run() -> list[str]:
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
            event = {
                "search_tags": ["memory_type=events"],
                "context_type": "memory",
                "account_id": "acme",
            }
            store._shared_adapter.upsert(
                [
                    {
                        **event,
                        "id": "old-event",
                        "uri": "viking://resources/old",
                        "vector": NEAREST,
                        "updated_at": (ORIGIN - timedelta(days=90)).isoformat(),
                    },
                    {
                        **event,
                        "id": "new-event",
                        "uri": "viking://resources/new",
                        "vector": NEAR,
                        "updated_at": ORIGIN.isoformat(),
                    },
                ]
            )
            ctx = RequestContext(user=UserIdentifier("acme", "bob"), role=Role.ROOT)
            rows = await store.search_in_tenant(
                ctx=ctx,
                query_vector=QUERY,
                limit=2,
                events_time_decay_protection="0",
                request_now=ORIGIN,
            )
            return [str(row["id"]) for row in rows]
        finally:
            await store.close()

    assert asyncio.run(run()) == ["new-event", "old-event"]
