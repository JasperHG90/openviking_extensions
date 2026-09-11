"""Tests for finding the files that belong in a sync."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from helpers import write

from ov_sync.config import ExcludeConfig, SyncConfig
from ov_sync.scanner import (
    file_digest,
    is_excluded,
    is_syncable,
    parse_frontmatter,
    scan,
)


def test_scan_finds_included_extensions_only(
    folder: Path, sync_config: SyncConfig
) -> None:
    """Only the configured extensions are picked up."""
    write(folder / "note.md", "hello")
    write(folder / "data.csv", "a,b")
    write(folder / "binary.bin", b"\x00\x01")

    found = [entry.relative_path for entry in scan(folder, sync_config)]

    assert found == ["data.csv", "note.md"]


def test_scan_returns_sorted_paths(folder: Path, sync_config: SyncConfig) -> None:
    """Output order does not depend on the filesystem's."""
    for name in ("z.md", "a.md", "m/inner.md"):
        write(folder / name, "x")

    found = [entry.relative_path for entry in scan(folder, sync_config)]

    assert found == ["a.md", "m/inner.md", "z.md"]


def test_scan_records_size_and_mtime(folder: Path, sync_config: SyncConfig) -> None:
    """Each entry carries the metadata the diff runs on."""
    path = write(folder / "note.md", "hello")

    entry = scan(folder, sync_config)[0]

    assert entry.size == path.stat().st_size
    assert entry.mtime == path.stat().st_mtime
    assert entry.path == path


def test_scan_skips_own_state_and_config(folder: Path, sync_config: SyncConfig) -> None:
    """ov-sync never syncs its own bookkeeping back into OpenViking."""
    config = SyncConfig(root_uri="viking://resources/notes", include_extensions=[])
    write(folder / sync_config.state_file, "sqlite")
    write(folder / f"{sync_config.state_file}-wal", "wal")
    write(folder / "ov-sync.toml", "[sync]")
    write(folder / "note.md", "hello")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["note.md"]


def test_empty_include_extensions_takes_everything(folder: Path) -> None:
    """An empty extension list means no extension filter at all."""
    config = SyncConfig(root_uri="viking://resources/notes", include_extensions=[])
    write(folder / "note.md", "hello")
    write(folder / "archive.tar.gz", b"\x1f\x8b")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["archive.tar.gz", "note.md"]


def test_scan_honors_base_exclusions(folder: Path, sync_config: SyncConfig) -> None:
    """A directory in the base exclude list is never walked into."""
    write(folder / ".git" / "config.md", "x")
    write(folder / "node_modules" / "pkg" / "readme.md", "x")
    write(folder / "keep.md", "x")

    found = [entry.relative_path for entry in scan(folder, sync_config)]

    assert found == ["keep.md"]


def test_scan_honors_ignore_folders_at_any_depth(folder: Path) -> None:
    """An ignored folder name matches wherever it appears."""
    config = SyncConfig(
        root_uri="viking://resources/notes",
        exclude=ExcludeConfig(ignore_folders=["private"]),
    )
    write(folder / "private" / "secret.md", "x")
    write(folder / "work" / "private" / "secret.md", "x")
    write(folder / "work" / "public.md", "x")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["work/public.md"]


def test_scan_honors_glob_exclusions(folder: Path) -> None:
    """extends_exclude patterns match a whole path or one component."""
    config = SyncConfig(
        root_uri="viking://resources/notes",
        exclude=ExcludeConfig(extends_exclude=["*.draft.md", "templates"]),
    )
    write(folder / "post.draft.md", "x")
    write(folder / "templates" / "daily.md", "x")
    write(folder / "post.md", "x")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["post.md"]


def test_scan_honors_frontmatter_skip_marker(
    folder: Path, sync_config: SyncConfig
) -> None:
    """A note carrying the skip marker is left out, whatever its case."""
    write(folder / "skipped.md", "---\nov: SKIP\ntitle: x\n---\n\nbody")
    write(folder / "kept.md", "---\nov: keep\n---\n\nbody")
    write(folder / "plain.md", "no frontmatter")

    found = [entry.relative_path for entry in scan(folder, sync_config)]

    assert found == ["kept.md", "plain.md"]


def test_skip_marker_only_applies_to_markdown(
    folder: Path, sync_config: SyncConfig
) -> None:
    """A csv that happens to start with a --- block is still synced."""
    write(folder / "table.csv", "---\nov: skip\n---\na,b")

    found = [entry.relative_path for entry in scan(folder, sync_config)]

    assert found == ["table.csv"]


def test_scan_does_not_follow_directory_symlinks(
    folder: Path, sync_config: SyncConfig
) -> None:
    """A link pointing at an ancestor must not send the scan round forever."""
    write(folder / "note.md", "x")
    (folder / "loop").symlink_to(folder, target_is_directory=True)

    found = [entry.relative_path for entry in scan(folder, sync_config)]

    assert found == ["note.md"]


def test_scan_skips_broken_symlinks(folder: Path, sync_config: SyncConfig) -> None:
    """A dangling link has nothing to send."""
    write(folder / "note.md", "x")
    (folder / "dangling.md").symlink_to(folder / "gone.md")

    found = [entry.relative_path for entry in scan(folder, sync_config)]

    assert found == ["note.md"]


def test_scan_rejects_a_missing_folder(tmp_path: Path, sync_config: SyncConfig) -> None:
    """A path that is not a directory fails loudly rather than syncing nothing."""
    with pytest.raises(NotADirectoryError):
        scan(tmp_path / "nope", sync_config)


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        ("notes/keep.md", False),
        (".git/config", True),
        ("deep/.git/config", True),
        ("node_modules/pkg/index.md", True),
    ],
)
def test_is_excluded(relative_path: str, expected: bool) -> None:
    """Base patterns match a component at any depth."""
    assert is_excluded(relative_path, ExcludeConfig()) is expected


def test_parse_frontmatter_reads_top_level_scalars() -> None:
    """Quoted values are unquoted and the block ends at the closing fence."""
    text = '---\ntitle: "My note"\nov: skip\n---\n\nov: not-frontmatter\n'

    assert parse_frontmatter(text) == {"title": "My note", "ov": "skip"}


def test_parse_frontmatter_without_a_block() -> None:
    """A file with no leading fence has no frontmatter."""
    assert parse_frontmatter("# Heading\n\nbody") == {}


def test_file_digest_tracks_content_not_mtime(folder: Path) -> None:
    """The same bytes hash the same however the timestamp moves."""
    path = write(folder / "note.md", "hello")
    before = file_digest(path)
    os.utime(path, (0, 0))

    assert file_digest(path) == before

    path.write_text("goodbye", encoding="utf-8")
    assert file_digest(path) != before


def test_the_sqlite_journal_is_never_synced(folder: Path) -> None:
    """SQLite writes `<db>-journal` beside the database on every commit.

    The default journal mode is `delete`, so `-journal` is the file that
    actually appears — `-wal` and `-shm` only exist under WAL.
    """
    config = SyncConfig(root_uri="viking://resources/notes", include_extensions=[])
    write(folder / ".ov-sync.db", "sqlite")
    write(folder / ".ov-sync.db-journal", "journal")
    write(folder / "note.md", "hello")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["note.md"]


def test_an_ignored_folder_prunes_the_walk(folder: Path) -> None:
    """Pruning is the point: an ignored tree should not be walked at all."""
    config = SyncConfig(
        root_uri="viking://resources/notes",
        exclude=ExcludeConfig(ignore_folders=["private"]),
    )
    write(folder / "private" / "secret.md", "x")

    assert is_excluded("private", config.exclude, is_dir=True) is True
    # As a file path, `private` is a filename and no folder name matches.
    assert is_excluded("private", config.exclude) is False
    assert scan(folder, config) == []


def test_is_syncable_matches_what_the_scan_keeps(
    folder: Path, sync_config: SyncConfig
) -> None:
    """The watcher filters events with it, so it has to agree with the scan."""
    write(folder / "note.md", "hello")
    write(folder / ".git" / "config.md", "x")

    assert is_syncable("note.md", sync_config) is True
    assert is_syncable(".git/config.md", sync_config) is False
    assert is_syncable(sync_config.state_file, sync_config) is False
    assert is_syncable(f"{sync_config.state_file}-journal", sync_config) is False
    assert is_syncable("photo.heic", sync_config) is False
