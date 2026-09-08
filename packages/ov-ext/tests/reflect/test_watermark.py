"""How far the sweep got, and what happens when that record is broken."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ov_ext.reflect.watermark import Watermark

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


def test_a_store_never_swept_admits_everything() -> None:
    """The epoch rather than now: a first run should reflect on what exists."""
    assert Watermark.beginning().last_seen == EPOCH


def test_a_watermark_round_trips_through_storage() -> None:
    mark = Watermark(last_seen=NOW, swept_at=NOW)
    assert Watermark.loads(mark.dumps()).last_seen == NOW


def test_a_missing_record_means_never_swept() -> None:
    assert Watermark.loads(None).last_seen == EPOCH
    assert Watermark.loads("").last_seen == EPOCH
    assert Watermark.loads("   ").last_seen == EPOCH


def test_a_corrupt_record_costs_one_sweep_not_every_sweep() -> None:
    """Re-reading everything is recoverable; refusing to start is not."""
    assert Watermark.loads("{not json").last_seen == EPOCH
    assert Watermark.loads('{"last_seen": "yesterday"}').last_seen == EPOCH


def test_the_mark_moves_forward_to_what_was_read() -> None:
    mark = Watermark.beginning().advanced_to(NOW, now=NOW)
    assert mark.last_seen == NOW
    assert mark.swept_at == NOW


def test_the_mark_never_moves_backwards() -> None:
    """A sweep that read only older rows must not re-open covered ground."""
    ahead = Watermark(last_seen=NOW, swept_at=NOW)
    assert ahead.advanced_to(NOW - timedelta(days=5), now=NOW).last_seen == NOW


def test_advancing_leaves_the_original_alone() -> None:
    original = Watermark.beginning()
    original.advanced_to(NOW, now=NOW)
    assert original.last_seen == EPOCH
