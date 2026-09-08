"""Numbering memories for the model, and resolving what it cites back."""

from __future__ import annotations

from datetime import datetime, timezone

from ov_ext.reflect.citations import build_memory_context, citation_map, parse_timestamp

from .helpers import row


def test_uris_are_numbered_densely_from_zero() -> None:
    forward, back = citation_map(["a", "b", "c"])
    assert forward == {"a": 0, "b": 1, "c": 2}
    assert back == {0: "a", 1: "b", 2: "c"}


def test_a_uri_seen_twice_keeps_its_first_number() -> None:
    """A memory found by both the change set and the neighbour search is one memory.

    Numbering it twice would give the model two indices for one text and let a
    single memory satisfy an evidence floor of two.
    """
    forward, back = citation_map(["a", "b", "a"])
    assert forward == {"a": 0, "b": 1}
    assert back == {0: "a", 1: "b"}


def test_context_is_built_in_index_order() -> None:
    rows = [row("b", "second"), row("a", "first")]
    forward, _ = citation_map(["a", "b"])
    contexts = build_memory_context(rows, forward)
    assert [c.index_id for c in contexts] == [0, 1]
    assert [c.content for c in contexts] == ["first", "second"]


def test_a_row_outside_the_map_is_skipped_rather_than_renumbered() -> None:
    """Silently numbering it would shift every index the caller can resolve."""
    contexts = build_memory_context([row("a", "x"), row("z", "y")], {"a": 0})
    assert [c.index_id for c in contexts] == [0]


def test_timestamps_come_back_aware_whatever_went_in() -> None:
    aware = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert parse_timestamp(aware) == aware
    assert parse_timestamp(datetime(2026, 9, 1)) == aware
    assert parse_timestamp("2026-09-01T00:00:00Z") == aware
    assert parse_timestamp("2026-09-01T00:00:00+00:00") == aware


def test_an_unreadable_timestamp_does_not_lose_the_memory() -> None:
    """Recency is a nice-to-have; dropping the memory over it is not."""
    assert parse_timestamp("not a date").tzinfo is timezone.utc
    assert parse_timestamp(None).tzinfo is timezone.utc
