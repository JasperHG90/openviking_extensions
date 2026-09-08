"""Dense integer ids for the model, URIs for us.

A model asked to cite a ``viking://`` URI will cheerfully invent a plausible
one, and the invention is expensive to detect because it looks exactly like a
real URI. Handing it ``0``, ``1``, ``2`` instead makes fabrication obvious --
an index outside the range is simply dropped -- and costs far fewer tokens than
repeating long paths through a prompt.

OpenViking does this internally too: ``PageIdMap`` numbers pages 1-99 for ones
that already exist and 100+ for ones the model proposes, so the range itself
says whether the model is referring or inventing. Reflection has no such split
-- every memory it shows the model already exists -- so this is the simpler
zero-based version.

Ported from memex's ``memory/reflect/utils.py``. ``create_citation_map`` and
``parse_timestamp`` are verbatim in behaviour; the types changed from UUID to
``str`` because OpenViking addresses by URI.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

__all__ = ["build_memory_context", "citation_map", "parse_timestamp"]

from .models import MemoryRow, ReflectMemoryContext


def citation_map(uris: Sequence[str]) -> tuple[dict[str, int], dict[int, str]]:
    """Number ``uris`` densely from zero, both ways.

    Duplicates collapse onto the first index they were given, so a memory that
    arrives from both the changed set and the neighbour search is one entry
    rather than two competing ones.

    Parameters
    ----------
    uris :
        Stored URIs, in the order they should be shown to the model.

    Returns
    -------
    tuple[dict[str, int], dict[int, str]]
        URI to index, and index back to URI.
    """
    uri_to_index: dict[str, int] = {}
    index_to_uri: dict[int, str] = {}
    for uri in uris:
        if uri in uri_to_index:
            continue
        index = len(uri_to_index)
        uri_to_index[uri] = index
        index_to_uri[index] = uri
    return uri_to_index, index_to_uri


def parse_timestamp(value: object) -> datetime:
    """Coerce ``value`` into a timezone-aware UTC datetime.

    Accepts a datetime, an ISO string (including the ``Z`` suffix that
    ``datetime.fromisoformat`` rejects before 3.11), or a number.

    A number is **epoch milliseconds**, which is how OpenViking encodes a
    ``date_time`` field -- ``parse_datetime_to_epoch_ms`` in the backends
    multiplies by 1000 on the way in, and the index hands the integer straight
    back. Reading it as seconds, or failing to read it at all, silently stamps
    every row with the current time: the watermark then advances to now on
    every sweep, and every memory is shown to the model as having happened
    today.

    Falls back to the current time rather than raising for anything else: a
    memory with an unreadable timestamp should still be reflected on, just
    without contributing to recency.

    Parameters
    ----------
    value :
        A datetime, an ISO-8601 string, epoch milliseconds, or anything else.

    Returns
    -------
    datetime
        Timezone-aware, in UTC.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, bool):
        # bool is an int subclass; treating True as 1ms past the epoch would be
        # a silent absurdity rather than an obvious one.
        return datetime.now(timezone.utc)

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.now(timezone.utc)

    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc)


def build_memory_context(
    rows: Sequence[MemoryRow],
    uri_to_index: dict[str, int],
) -> list[ReflectMemoryContext]:
    """Reduce rows to what the model sees, numbered by ``uri_to_index``.

    Rows whose URI is not in the map are skipped rather than given a fresh
    number, so the indices the model is shown always match the ones the caller
    can resolve afterwards.

    Parameters
    ----------
    rows :
        The memories to show.
    uri_to_index :
        Mapping from :func:`citation_map`.

    Returns
    -------
    list[ReflectMemoryContext]
        In ascending index order, so the model reads them numbered 0, 1, 2.
    """
    contexts = [
        ReflectMemoryContext(
            index_id=uri_to_index[row.uri],
            content=row.text,
            occurred=row.created_at.isoformat(),
        )
        for row in rows
        if row.uri in uri_to_index
    ]
    contexts.sort(key=lambda context: context.index_id)
    return contexts
