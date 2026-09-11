"""Find the files in a folder that belong in OpenViking.

The scanner answers one question — what is on disk right now — and answers it
cheaply. It reads each file's size and mtime but not its contents, except for
the small frontmatter probe that honors a skip marker. Deciding what actually
changed is the engine's job, and it reads content only for the candidates the
diff turns up.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .config import CONFIG_FILENAME, ExcludeConfig, SyncConfig

# A leading ---\n ... \n--- block. Deliberately not a YAML parse: the only
# thing read out of it is one scalar, and a parser would be a dependency
# earning nothing.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)

# How much of a file to read when probing for frontmatter. A block longer than
# this is not a header any more.
_FRONTMATTER_PROBE_BYTES = 8192

_DIGEST_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class LocalFile:
    """A file on disk that is in scope for syncing.

    Attributes
    ----------
    path : Path
        Absolute path.
    relative_path : str
        Path relative to the synced folder, with forward slashes on every
        platform, so it maps onto a viking:// URI unchanged.
    mtime : float
        Modification time, as a Unix timestamp.
    size : int
        Size in bytes.
    """

    path: Path
    relative_path: str
    mtime: float
    size: int


def parse_frontmatter(text: str) -> dict[str, str]:
    """Pull the top-level scalars out of a leading YAML frontmatter block.

    Nested YAML is dropped and values keep their case; a caller comparing
    case-insensitively lowers them itself.

    Parameters
    ----------
    text :
        Start of a file's contents.

    Returns
    -------
    dict[str, str]
        The block's ``key: value`` pairs, empty when there is no block.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}

    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.strip().partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def file_digest(path: Path) -> str:
    """Return the SHA-256 of a file's bytes, as hex.

    Used to tell a real edit from a touched mtime. Reading a file to decide
    whether to send it is far cheaper than sending it: every write makes the
    server re-embed the file.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def is_excluded(
    relative_path: str, exclude: ExcludeConfig, *, is_dir: bool = False
) -> bool:
    """Whether a relative path is covered by the exclusion rules.

    Parameters
    ----------
    relative_path :
        Path relative to the synced folder, forward-slashed.
    exclude :
        The configured rules.
    is_dir :
        Whether the path names a directory. It decides how ``ignore_folders``
        reads the last component: for a file that is the filename, which is
        not a folder name; for a directory it is the folder being tested, and
        without this the walk cannot prune an ignored tree by name.

    Returns
    -------
    bool
        True when the path matches an ignored folder name, or any glob pattern
        either as a whole or in one of its components.
    """
    parts = relative_path.split("/")
    folders = parts if is_dir else parts[:-1]
    if exclude.ignore_folders and any(part in exclude.ignore_folders for part in folders):
        return True

    for pattern in exclude.all_patterns:
        if fnmatch(relative_path, pattern) or any(
            fnmatch(part, pattern) for part in parts
        ):
            return True
    return False


def own_files(sync_config: SyncConfig) -> frozenset[str]:
    """Return the files ov-sync keeps in the folder and must never sync.

    Syncing ov-sync's own bookkeeping into OpenViking helps nobody, and the
    SQLite journal is worse than useless: it appears and disappears on every
    commit, so a watch loop that noticed it would keep waking itself up.

    Parameters
    ----------
    sync_config :
        Supplies the configured state filename.

    Returns
    -------
    frozenset[str]
        Relative paths to skip.
    """
    state_file = sync_config.state_file
    return frozenset(
        {
            state_file,
            # One per journal mode. The default is `delete`, which writes
            # `-journal`; `-wal` and `-shm` appear only under WAL.
            f"{state_file}-journal",
            f"{state_file}-wal",
            f"{state_file}-shm",
            CONFIG_FILENAME,
        }
    )


def is_syncable(relative_path: str, sync_config: SyncConfig) -> bool:
    """Whether a path is one this sync would send, judged on its name alone.

    This is the cheap half of the scanner's filter — everything that can be
    decided without opening the file. The watcher uses it to tell an event
    worth acting on from one to drop.

    Parameters
    ----------
    relative_path :
        Path relative to the synced folder, forward-slashed.
    sync_config :
        Extension filter and exclusion rules.

    Returns
    -------
    bool
        True when the path is in scope.
    """
    if relative_path in own_files(sync_config):
        return False
    extensions = sync_config.include_extensions
    if extensions and Path(relative_path).suffix.lower() not in extensions:
        return False
    return not is_excluded(relative_path, sync_config.exclude)


def has_skip_marker(path: Path, exclude: ExcludeConfig) -> bool:
    """Whether a Markdown file's frontmatter asks to be left out.

    Only ``.md`` files are probed: nothing else in a sync carries frontmatter,
    and reading the head of every PDF to find out would be wasted I/O.
    """
    if path.suffix.lower() != ".md":
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(_FRONTMATTER_PROBE_BYTES)
    except OSError:
        return False
    value = parse_frontmatter(head).get(exclude.frontmatter_skip_key, "")
    return value.lower() == exclude.frontmatter_skip_value.lower()


def scan(folder: Path, sync_config: SyncConfig) -> list[LocalFile]:
    """List the files in ``folder`` that are in scope for syncing.

    Parameters
    ----------
    folder :
        Root of the synced folder.
    sync_config :
        Supplies the extension filter and the exclusion rules. The state
        database and the config file are always skipped, whatever it says —
        syncing ov-sync's own bookkeeping back into OpenViking helps nobody.

    Returns
    -------
    list[LocalFile]
        One entry per file, sorted by relative path so a run's output is
        reproducible.

    Raises
    ------
    NotADirectoryError
        If ``folder`` does not exist or is not a directory.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    found: list[LocalFile] = []
    # os.walk rather than rglob, for two reasons: it never follows directory
    # symlinks, so a link pointing at an ancestor cannot loop forever; and
    # pruning `dirnames` skips an excluded tree instead of walking into it and
    # discarding every file it holds.
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(dirpath)
        relative_dir = here.relative_to(folder).as_posix()
        prefix = "" if relative_dir == "." else f"{relative_dir}/"
        dirnames[:] = [
            name
            for name in dirnames
            if not is_excluded(f"{prefix}{name}", sync_config.exclude, is_dir=True)
        ]

        for name in filenames:
            relative_path = f"{prefix}{name}"
            if not is_syncable(relative_path, sync_config):
                continue
            path = here / name
            # A symlinked file is fine — its target gets read and sent. A
            # broken one is not, and stat() is where that shows up.
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            if has_skip_marker(path, sync_config.exclude):
                continue

            found.append(
                LocalFile(
                    path=path,
                    relative_path=relative_path,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                )
            )

    return sorted(found, key=lambda entry: entry.relative_path)
