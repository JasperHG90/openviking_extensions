"""OpenViking's own local backend, used as the oracle for filter semantics.

OpenViking 0.4.x shipped a Python filter evaluator (``cuvs_index.matches_filter``)
that these tests compared against. 0.5 removed it: every built-in backend now
evaluates filters in the native engine. So the oracle is that engine, reached
the way OpenViking reaches it -- through ``LocalCollectionAdapter``, in memory.

Records and filters go through the same adapter methods on both sides
(``upsert`` and ``query``), so URI encoding, default filling and filter
compilation are OpenViking's own on the reference side.

The engine is an oracle only for a filter it can parse. One it cannot -- a
``{"filter": ...}`` wrapper, ``None`` among the operands, an empty operand
list, an operand of the wrong type -- is dropped with a log line, and the
query returns every row. That is a failure mode, not a meaning, and this
backend deliberately does not copy it. :meth:`LocalReference.evaluates` tells
the two apart, so a test compares only what the engine really evaluated.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from openviking.storage.vectordb_adapters.local_adapter import LocalCollectionAdapter

# More rows than any test writes, so a filter-only query returns every match.
_ALL_ROWS = 10_000


class LocalReference:
    """An in-memory local collection that mirrors the records a test writes.

    Parameters
    ----------
    meta :
        The collection schema, the same one the backend under test was given.
    """

    def __init__(self, meta: dict[str, Any]) -> None:
        name = str(meta["CollectionName"])
        self._impossible = _impossible_node(meta)
        self._everything = {**self._impossible, "op": "must_not"}
        self._adapter = LocalCollectionAdapter(
            collection_name=name, project_path="", index_name="default"
        )
        self._adapter.create_collection(
            name, meta, distance="cosine", sparse_weight=0.0, index_name="default"
        )

    def upsert(self, records: dict[str, Any] | Iterable[dict[str, Any]]) -> None:
        """Write the records, copied so the caller's dicts are left alone."""
        batch = [records] if isinstance(records, dict) else list(records)
        self._adapter.upsert([dict(record) for record in batch])

    def matching_ids(self, node: dict[str, Any]) -> set[str]:
        """Return the ids of every row the filter selects."""
        return {
            str(row["id"]) for row in self._adapter.query(filter=node, limit=_ALL_ROWS)
        }

    def evaluates(self, node: dict[str, Any]) -> bool:
        """Return whether the engine really evaluates ``node``.

        Checked through identities that hold for any real filter ``X``, with
        ``F`` a condition no row meets and ``T`` one every row meets, in both
        operand orders: ``X and F`` selects nothing, ``X or F`` selects what
        ``X`` does, and ``X or T`` selects every row. An engine that dropped
        ``X`` breaks the first; one that dropped only the unparseable branch of
        an ``or`` breaks the second; one that lets ``X`` empty a whole ``or``
        breaks the third. Each of those was seen in OpenViking 0.5.6.
        """
        alone = self.matching_ids(node)
        every = self.matching_ids(self._everything)
        for pair in ([node, self._impossible], [self._impossible, node]):
            if self.matching_ids({"op": "and", "conds": pair}):
                return False
            if self.matching_ids({"op": "or", "conds": pair}) != alone:
                return False
        for pair in ([node, self._everything], [self._everything, node]):
            if self.matching_ids({"op": "or", "conds": pair}) != every:
                return False
        return True

    def close(self) -> None:
        """Release the in-memory collection."""
        self._adapter.close()


def _impossible_node(meta: dict[str, Any]) -> dict[str, Any]:
    """Return a well-formed filter that no row in the collection can match.

    Built on a scalar-indexed text field, which the engine always parses, with
    a value no test writes.
    """
    types = {f["FieldName"]: f["FieldType"] for f in meta["Fields"]}
    field = next(
        name
        for name in meta.get("ScalarIndex", [])
        if types.get(name) in ("string", "text")
    )
    return {"op": "must", "field": field, "conds": ["no row has this value"]}


def reference_ids(
    meta: dict[str, Any],
    records: dict[str, Any] | Iterable[dict[str, Any]],
    node: dict[str, Any],
) -> set[str]:
    """Return the ids the local backend selects for ``node`` over ``records``.

    Parameters
    ----------
    meta :
        The collection schema.
    records :
        The records the backend under test was given.
    node :
        The filter to evaluate.

    Returns
    -------
    set[str]
        The ids of the matching records.

    Raises
    ------
    AssertionError
        If the engine does not evaluate ``node``, so has no answer to give.
    """
    reference = LocalReference(meta)
    try:
        reference.upsert(records)
        assert reference.evaluates(node), f"the engine does not evaluate {node!r}"
        return reference.matching_ids(node)
    finally:
        reference.close()


def engine_answer(
    meta: dict[str, Any],
    records: dict[str, Any] | Iterable[dict[str, Any]],
    node: dict[str, Any],
) -> set[str] | None:
    """Return the ids the engine selects, or ``None`` if it does not evaluate ``node``.

    For a test that pins this backend's answer explicitly and wants the engine
    to confirm it wherever the engine has an answer to give.

    Parameters
    ----------
    meta :
        The collection schema.
    records :
        The records the backend under test was given.
    node :
        The filter to evaluate.

    Returns
    -------
    set[str] | None
        The matching ids, or ``None`` when the engine would drop the filter.
    """
    reference = LocalReference(meta)
    try:
        reference.upsert(records)
        return reference.matching_ids(node) if reference.evaluates(node) else None
    finally:
        reference.close()
