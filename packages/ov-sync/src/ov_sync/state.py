"""The local record of what has already been sent to OpenViking.

One SQLite file per synced folder, kept in the folder's root. It holds a row
per file — where it landed, and what it looked like when it went — plus a
single metadata row. Delete the file and the next run re-sends everything,
which is harmless: every write is an upsert keyed by the target URI.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from .scanner import LocalFile

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_files (
    relative_path TEXT PRIMARY KEY,
    uri           TEXT NOT NULL,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    digest        TEXT NOT NULL,
    synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_meta (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    last_sync TEXT,
    root_uri  TEXT
);
"""


class RootUriMismatch(Exception):
    """Raised when a folder is synced to a different root than last time.

    Every stored URI was built from the old root, so a silent switch would
    orphan the whole previous sync in OpenViking: the old files would stay
    where they were, untracked, and the new root would fill up with copies.
    """


@dataclass(frozen=True)
class SyncedFile:
    """What the last sync recorded about one file.

    Attributes
    ----------
    relative_path : str
        Path relative to the synced folder, forward-slashed.
    uri : str
        Where it was written in OpenViking.
    mtime : float
        Modification time at the time of the sync.
    size : int
        Size in bytes at the time of the sync.
    digest : str
        SHA-256 of the bytes that were sent, as hex.
    synced_at : str
        ISO 8601 timestamp of the sync, in UTC.
    """

    relative_path: str
    uri: str
    mtime: float
    size: int
    digest: str
    synced_at: str


@dataclass(frozen=True)
class StateDiff:
    """What the folder looks like now, against what the state says was sent.

    Attributes
    ----------
    added : list[LocalFile]
        Files with no row in the state.
    candidates : list[LocalFile]
        Files whose size or mtime moved since the last sync. Whether the
        contents actually changed is settled by hashing them, which the engine
        does — an mtime alone is not evidence of an edit.
    unchanged : list[LocalFile]
        Files whose size and mtime both match the state.
    deleted : list[SyncedFile]
        Rows whose file is no longer on disk.
    """

    added: list[LocalFile]
    candidates: list[LocalFile]
    unchanged: list[LocalFile]
    deleted: list[SyncedFile]


class SyncState:
    """SQLite-backed record of what has been sent.

    Use it as a context manager so the connection is closed even when a sync
    fails part-way.

    Parameters
    ----------
    db_path :
        Where the SQLite file lives. Created, with its schema, if absent.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._connection = sqlite3.connect(db_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._connection.commit()

    def __enter__(self) -> SyncState:
        """Return the open state, for use in a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection on the way out of a ``with`` block."""
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        self._connection.close()

    @property
    def path(self) -> Path:
        """Where this state lives on disk."""
        return self._path

    @property
    def last_sync(self) -> str | None:
        """ISO 8601 timestamp of the last run that wrote anything, or None."""
        row = self._connection.execute(
            "SELECT last_sync FROM sync_meta WHERE id = 1"
        ).fetchone()
        return None if row is None else row["last_sync"]

    @property
    def root_uri(self) -> str | None:
        """The root this folder was last synced to, or None on a fresh state."""
        row = self._connection.execute(
            "SELECT root_uri FROM sync_meta WHERE id = 1"
        ).fetchone()
        return None if row is None else row["root_uri"]

    def check_root(self, root_uri: str) -> None:
        """Check a root against the one this folder was last synced to.

        Writes nothing, so a dry run can raise the same error a real run
        would without committing the caller to the root it was asked about.

        Parameters
        ----------
        root_uri :
            The target root for this run.

        Raises
        ------
        RootUriMismatch
            If the state was built against a different root.
        """
        current = self.root_uri
        if current is not None and current != root_uri:
            raise RootUriMismatch(
                f"This folder was last synced to {current}, not {root_uri}. "
                f"To move it, delete {self._path.name} and run a full sync — "
                "and remove the old tree in OpenViking yourself."
            )

    def bind_root(self, root_uri: str) -> None:
        """Record the root this folder syncs to, after checking it matches.

        Parameters
        ----------
        root_uri :
            The target root for this run.

        Raises
        ------
        RootUriMismatch
            If the state was built against a different root.
        """
        self.check_root(root_uri)
        self._connection.execute(
            "INSERT INTO sync_meta (id, root_uri) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET root_uri = excluded.root_uri",
            (root_uri,),
        )
        self._connection.commit()

    def all_files(self) -> dict[str, SyncedFile]:
        """Return every tracked file, keyed by relative path."""
        rows = self._connection.execute(
            "SELECT relative_path, uri, mtime, size, digest, synced_at FROM synced_files"
        ).fetchall()
        return {
            row["relative_path"]: SyncedFile(
                relative_path=row["relative_path"],
                uri=row["uri"],
                mtime=row["mtime"],
                size=row["size"],
                digest=row["digest"],
                synced_at=row["synced_at"],
            )
            for row in rows
        }

    def mark_synced(self, entries: Iterable[tuple[LocalFile, str, str]]) -> None:
        """Record files as sent, and stamp the run.

        Parameters
        ----------
        entries :
            One ``(file, uri, digest)`` triple per file that reached the
            server. Anything that failed is left out, so the next run retries
            it.
        """
        now = datetime.now(UTC).isoformat()
        rows = [
            (entry.relative_path, uri, entry.mtime, entry.size, digest, now)
            for entry, uri, digest in entries
        ]
        if not rows:
            return
        self._connection.executemany(
            "INSERT INTO synced_files "
            "(relative_path, uri, mtime, size, digest, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(relative_path) DO UPDATE SET "
            "uri = excluded.uri, mtime = excluded.mtime, size = excluded.size, "
            "digest = excluded.digest, synced_at = excluded.synced_at",
            rows,
        )
        self._connection.execute(
            "INSERT INTO sync_meta (id, last_sync) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_sync = excluded.last_sync",
            (now,),
        )
        self._connection.commit()

    def touch(self, entries: Iterable[LocalFile]) -> None:
        """Update the recorded mtime and size without sending anything.

        This is for a file whose timestamp moved but whose bytes did not — a
        checkout, a sync client, a `touch`. Storing the new mtime stops the
        next run from hashing it all over again.

        Parameters
        ----------
        entries :
            Files whose contents matched their stored digest.
        """
        rows = [(entry.mtime, entry.size, entry.relative_path) for entry in entries]
        if not rows:
            return
        self._connection.executemany(
            "UPDATE synced_files SET mtime = ?, size = ? WHERE relative_path = ?",
            rows,
        )
        self._connection.commit()

    def forget(self, relative_paths: Sequence[str]) -> None:
        """Drop rows for files that are no longer tracked.

        Parameters
        ----------
        relative_paths :
            Paths to remove from the state.
        """
        if not relative_paths:
            return
        self._connection.executemany(
            "DELETE FROM synced_files WHERE relative_path = ?",
            [(path,) for path in relative_paths],
        )
        self._connection.commit()


def diff(state: SyncState, files: Sequence[LocalFile]) -> StateDiff:
    """Compare what is on disk against what the state says was sent.

    Parameters
    ----------
    state :
        The folder's sync state.
    files :
        What the scanner found.

    Returns
    -------
    StateDiff
        The four buckets: added, candidates for re-send, unchanged, deleted.
    """
    tracked = state.all_files()
    present = {entry.relative_path for entry in files}

    added: list[LocalFile] = []
    candidates: list[LocalFile] = []
    unchanged: list[LocalFile] = []
    for entry in files:
        previous = tracked.get(entry.relative_path)
        if previous is None:
            added.append(entry)
        elif previous.size != entry.size or previous.mtime != entry.mtime:
            candidates.append(entry)
        else:
            unchanged.append(entry)

    deleted = [row for path, row in tracked.items() if path not in present]
    return StateDiff(
        added=added,
        candidates=candidates,
        unchanged=unchanged,
        deleted=sorted(deleted, key=lambda row: row.relative_path),
    )
