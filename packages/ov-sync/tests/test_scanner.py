"""Tests for finding the files that belong in a sync."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import write

from ov_sync.config import ExcludeConfig, SyncConfig
from ov_sync.scanner import (
    file_digest,
    is_excluded,
    is_included,
    is_syncable,
    missing_include_folders,
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


def test_include_folders_limits_the_scan(folder: Path) -> None:
    """Only files under an included folder are found, at any depth below it."""
    config = SyncConfig(
        root_uri="viking://resources/notes",
        include_folders=["projects", "areas/work"],
    )
    write(folder / "top.md", "x")
    write(folder / "projects" / "a.md", "x")
    write(folder / "projects" / "deep" / "b.md", "x")
    write(folder / "areas" / "home.md", "x")
    write(folder / "areas" / "work" / "c.md", "x")
    write(folder / "areas" / "personal" / "d.md", "x")
    write(folder / "projects-old" / "e.md", "x")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["areas/work/c.md", "projects/a.md", "projects/deep/b.md"]


def test_excludes_still_apply_inside_an_included_folder(folder: Path) -> None:
    """Including a folder does not bring back what the exclude rules leave out."""
    config = SyncConfig(
        root_uri="viking://resources/notes",
        include_folders=["projects"],
        exclude=ExcludeConfig(ignore_folders=["private"]),
    )
    write(folder / "projects" / "keep.md", "x")
    write(folder / "projects" / "private" / "secret.md", "x")
    write(folder / "projects" / ".git" / "config.md", "x")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["projects/keep.md"]


@pytest.mark.parametrize(
    ("relative_path", "is_dir", "expected"),
    [
        ("areas", True, True),  # on the way to areas/work
        ("areas/work", True, True),
        ("areas/work/sub", True, True),
        ("areas/personal", True, False),
        ("areas/home.md", False, False),  # a file directly in an ancestor
        ("areas/work/c.md", False, True),
        ("areas/work", False, False),  # a file named like the folder
        ("areas/workshop", True, False),  # a prefix of the name is not a match
    ],
)
def test_is_included(relative_path: str, is_dir: bool, expected: bool) -> None:
    """Ancestors let the walk through; only paths below a folder are sent."""
    config = SyncConfig(
        root_uri="viking://resources/notes", include_folders=["areas/work"]
    )

    assert is_included(relative_path, config, is_dir=is_dir) is expected


def test_no_include_folders_means_everything() -> None:
    """The default is the whole folder, as before the option existed."""
    config = SyncConfig(root_uri="viking://resources/notes")

    assert is_included("anything.md", config) is True
    assert is_syncable("top.md", config) is True


def test_is_syncable_honors_include_folders() -> None:
    """The watcher filters with is_syncable, so it must agree with the scan."""
    config = SyncConfig(root_uri="viking://resources/notes", include_folders=["projects"])

    assert is_syncable("projects/a.md", config) is True
    assert is_syncable("top.md", config) is False
    assert is_syncable("other/a.md", config) is False


def test_the_synced_folder_itself_is_included() -> None:
    """A watch event on the root must not be dropped as out of scope."""
    config = SyncConfig(root_uri="viking://resources/notes", include_folders=["projects"])

    assert is_included(".", config, is_dir=True) is True
    assert is_included("", config, is_dir=True) is True


def test_the_walk_never_enters_a_folder_outside_the_includes(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning is the point: a large tree outside the includes is not walked."""
    config = SyncConfig(root_uri="viking://resources/notes", include_folders=["projects"])
    write(folder / "projects" / "a.md", "x")
    write(folder / "archive" / "deep" / "b.md", "x")
    seen: list[str] = []
    real_walk = os.walk

    def spy(
        top: Path, followlinks: bool = False
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        """Record each directory the walk visits."""
        for dirpath, dirnames, filenames in real_walk(top, followlinks=followlinks):
            seen.append(Path(dirpath).relative_to(folder.resolve()).as_posix())
            yield dirpath, dirnames, filenames

    monkeypatch.setattr("ov_sync.scanner.os.walk", spy)

    scan(folder, config)

    assert "archive" not in seen
    assert "projects" in seen


def test_missing_include_folders_names_what_is_not_there(folder: Path) -> None:
    """Every entry has to be a real folder, spelled as the disk spells it."""
    (folder / "Projects").mkdir()
    (folder / "areas" / "work").mkdir(parents=True)
    write(folder / "notes.md", "x")
    (folder / "linked").symlink_to(folder / "areas", target_is_directory=True)
    config = SyncConfig(
        root_uri="viking://resources/notes",
        include_folders=["projects", "areas/work", "areas/home", "notes.md", "linked"],
    )

    # "projects" is the wrong case, "notes.md" is a file, and the walk does
    # not follow the "linked" symlink, so none of the three can sync a thing.
    assert missing_include_folders(folder, config) == [
        "projects",
        "areas/home",
        "notes.md",
        "linked",
    ]


def test_no_include_folders_means_none_missing(folder: Path) -> None:
    """The whole folder is always there."""
    assert missing_include_folders(folder, SyncConfig(root_uri="viking://r/n")) == []


def test_an_include_with_glob_characters_matches_literally(folder: Path) -> None:
    """A folder called "Notes [2024]" is a name, not a pattern."""
    config = SyncConfig(
        root_uri="viking://resources/notes", include_folders=["Notes [2024]"]
    )
    write(folder / "Notes [2024]" / "a.md", "x")
    write(folder / "Notes 2" / "b.md", "x")

    found = [entry.relative_path for entry in scan(folder, config)]

    assert found == ["Notes [2024]/a.md"]
    assert missing_include_folders(folder, config) == []
