"""Drive ov-postgres 0.6.0 on OpenViking 0.4.17.1 against a reconciled table.

Run by ``tests/test_downgrade.py`` with the interpreter of a separate virtual
environment holding those two old releases, never by pytest itself: it must
import nothing from this checkout. Reads its arguments as JSON from argv[1]
and prints what it saw as JSON on stdout.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Embedding:
    """The one embedding attribute the 0.4 adapter reads from global config."""

    dimension: int


@dataclass(frozen=True)
class _Config:
    """Stand-in for OpenViking 0.4's global config, as its own tests used."""

    embedding: _Embedding


def vec(seed: int, dim: int) -> list[float]:
    """Return a deterministic pseudo-random vector."""
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(dim)]


def main() -> None:
    """Read, filter and write through the old adapter, and report."""
    args: dict[str, Any] = json.loads(sys.argv[1])
    dim = int(args["dim"])

    from openviking.storage.collection_schemas import CollectionSchemas
    from openviking.storage.vectordb_adapters import base
    from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

    from ov_postgres.adapter import PgVectorCollectionAdapter

    base.get_openviking_config = lambda: _Config(_Embedding(dim))
    adapter = PgVectorCollectionAdapter.from_config(
        VectorDBBackendConfig(
            backend="ov_postgres.adapter.PgVectorCollectionAdapter",
            name="context",
            index_name="default",
            distance_metric="cosine",
            custom_params={"dsn": args["dsn"], "schema": args["schema"]},
        )
    )
    created = adapter.create_collection(
        "context",
        CollectionSchemas.context_collection("context", dim),
        distance="cosine",
        sparse_weight=0.0,
        index_name="default",
    )
    report: dict[str, Any] = {"created": created}

    every = adapter.query(
        filter={"op": "must", "field": "account_id", "conds": ["acme"]},
        output_fields=["id", "uri", "level", "acl_mode", "md5"],
        limit=100,
    )
    report["seen"] = {row["id"]: row for row in every}
    report["level_2"] = sorted(
        row["id"]
        for row in adapter.query(
            filter={"op": "must", "field": "level", "conds": [2]}, limit=100
        )
    )
    nearest = adapter.query(query_vector=vec(1, dim), limit=1)
    report["nearest"] = nearest[0]["id"] if nearest else None

    adapter.upsert(
        {
            "id": "old-write",
            "uri": "viking://resources/team/old-write.md",
            "account_id": "acme",
            "level": 2,
            "context_type": "resource",
            "vector": vec(7, dim),
        }
    )
    adapter.upsert({**adapter.get(["new-open"])[0], "active_count": 5})
    # Re-adding a file, as OpenViking 0.4 does: a whole new record, with
    # nothing about ACL or md5, over one this version restricted.
    adapter.upsert(
        {
            "id": "new-secret",
            "uri": "viking://resources/team/new-secret.md",
            "account_id": "acme",
            "level": 2,
            "context_type": "resource",
            "abstract": "rewritten by the old stack",
            "vector": vec(9, dim),
        }
    )
    report["count"] = adapter.count()
    report["deleted"] = adapter.delete(ids=["to-delete"])
    adapter.close()
    print(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
