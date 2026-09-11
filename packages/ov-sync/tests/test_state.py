"""Tests for the local record of what has been sent."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import write

from ov_sync.config import SyncConfig
from ov_sync.scanner import scan
from ov_sync.state import RootUriMismatch, SyncState, diff

ROOT = "viking://resources/notes"


@pytest.fixture
def state(tmp_path: Path) -> Iterator[SyncState]:
    """An open state database in a temporary location."""
    with SyncState(tmp_path / "state.db") as open_state:
        yield open_state


def test_fresh_state_is_empty(state: SyncState) -> None:
    """A new database tracks nothing and remembers no run."""
    assert state.all_files() == {}
    assert state.last_sync is None
    assert state.root_uri is None


def test_bind_root_records_the_target(state: SyncState) -> None:
    """The first bind stores the root; a matching one is a no-op."""
    state.bind_root(ROOT)
    state.bind_root(ROOT)

    assert state.root_uri == ROOT


def test_bind_root_rejects_a_different_target(state: SyncState) -> None:
    """Switching roots would orphan the previous sync, so it is refused."""
    state.bind_root(ROOT)

    with pytest.raises(RootUriMismatch, match="was last synced to"):
        state.bind_root("viking://resources/elsewhere")


def test_mark_synced_records_files_and_stamps_the_run(
    folder: Path, state: SyncState, sync_config: SyncConfig
) -> None:
    """A synced file is tracked with the digest that was sent."""
    write(folder / "note.md", "hello")
    entry = scan(folder, sync_config)[0]

    state.mark_synced([(entry, f"{ROOT}/note.md", "abc123")])

    tracked = state.all_files()
    assert set(tracked) == {"note.md"}
    assert tracked["note.md"].uri == f"{ROOT}/note.md"
    assert tracked["note.md"].digest == "abc123"
    assert tracked["note.md"].size == entry.size
    assert state.last_sync is not None


def test_mark_synced_updates_an_existing_row(
    folder: Path, state: SyncState, sync_config: SyncConfig
) -> None:
    """Re-syncing a file replaces its row rather than adding a second one."""
    path = write(folder / "note.md", "hello")
    state.mark_synced([(scan(folder, sync_config)[0], f"{ROOT}/note.md", "first")])

    path.write_text("goodbye", encoding="utf-8")
    state.mark_synced([(scan(folder, sync_config)[0], f"{ROOT}/note.md", "second")])

    tracked = state.all_files()
    assert len(tracked) == 1
    assert tracked["note.md"].digest == "second"


def test_mark_synced_with_nothing_does_not_stamp_a_run(state: SyncState) -> None:
    """An empty sync is not a sync, so last_sync stays where it was."""
    state.mark_synced([])

    assert state.last_sync is None


def test_touch_updates_metadata_without_the_digest(
    folder: Path, state: SyncState, sync_config: SyncConfig
) -> None:
    """A moved mtime is recorded so the next run does not hash it again."""
    path = write(folder / "note.md", "hello")
    first = scan(folder, sync_config)[0]
    state.mark_synced([(first, f"{ROOT}/note.md", "digest")])

    path.touch()
    moved = scan(folder, sync_config)[0]
    state.touch([moved])

    tracked = state.all_files()["note.md"]
    assert tracked.mtime == moved.mtime
    assert tracked.digest == "digest"


def test_forget_drops_rows(
    folder: Path, state: SyncState, sync_config: SyncConfig
) -> None:
    """Forgetting a path stops it being tracked."""
    write(folder / "note.md", "hello")
    state.mark_synced([(scan(folder, sync_config)[0], f"{ROOT}/note.md", "d")])

    state.forget(["note.md"])

    assert state.all_files() == {}


def test_diff_sorts_files_into_buckets(
    folder: Path, state: SyncState, sync_config: SyncConfig
) -> None:
    """New, moved, untouched and vanished files each land in their own bucket."""
    write(folder / "unchanged.md", "same")
    write(folder / "moved.md", "before")
    write(folder / "vanished.md", "gone soon")
    tracked = {entry.relative_path: entry for entry in scan(folder, sync_config)}
    state.mark_synced([(entry, f"{ROOT}/{path}", "d") for path, entry in tracked.items()])

    (folder / "moved.md").write_text("after and longer", encoding="utf-8")
    (folder / "vanished.md").unlink()
    write(folder / "added.md", "brand new")

    result = diff(state, scan(folder, sync_config))

    assert [entry.relative_path for entry in result.added] == ["added.md"]
    assert [entry.relative_path for entry in result.candidates] == ["moved.md"]
    assert [entry.relative_path for entry in result.unchanged] == ["unchanged.md"]
    assert [row.relative_path for row in result.deleted] == ["vanished.md"]


def test_diff_treats_a_touched_file_as_a_candidate(
    folder: Path, state: SyncState, sync_config: SyncConfig
) -> None:
    """A moved mtime makes a file a candidate; hashing decides the rest."""
    path = write(folder / "note.md", "hello")
    state.mark_synced([(scan(folder, sync_config)[0], f"{ROOT}/note.md", "d")])
    os.utime(path, (0, 0))

    result = diff(state, scan(folder, sync_config))

    assert [entry.relative_path for entry in result.candidates] == ["note.md"]


def test_state_survives_being_reopened(
    tmp_path: Path, folder: Path, sync_config: SyncConfig
) -> None:
    """The point of the file is that the next run reads it."""
    db_path = tmp_path / "state.db"
    write(folder / "note.md", "hello")
    with SyncState(db_path) as first:
        first.bind_root(ROOT)
        first.mark_synced([(scan(folder, sync_config)[0], f"{ROOT}/note.md", "d")])

    with SyncState(db_path) as second:
        assert second.root_uri == ROOT
        assert set(second.all_files()) == {"note.md"}
