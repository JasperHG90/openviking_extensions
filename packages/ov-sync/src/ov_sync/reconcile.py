"""Adopt files OpenViking already holds, instead of sending them again.

The state file is the only record of what has been sent, and a run that fails
part-way writes nothing for the batches it lost. That is the safe choice at the
time — a batch-write that times out does not say which of its files landed — but
it leaves the two sides disagreeing in a way no amount of re-running settles: a
sync whose batches keep timing out re-uploads the same thousands of files on
every attempt, and records none of them.

This is the way out. For each file the sync is about to send, it reads back what
the server actually holds at that URI and compares the bytes. Where they match,
the file is written into the state as sent, because it was. Nothing is uploaded.

The comparison is the file's SHA-256 against the stored copy's, never a
timestamp or a size on its own. A state row says "these exact bytes are at this
URI", and a row seeded on weaker evidence would be wrong in the one direction
that costs data: a file the sync then skips forever.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from pathlib import Path

from pydantic import BaseModel, Field

from .client import OvClient, OvError, ServerFile
from .config import SyncConfig
from .engine import ProgressCallback, target_uri
from .scanner import LocalFile, read_stable
from .state import SyncState
from .uri_path import safe_relative_path

# How many adoptions are written to the state at once. A reconcile over a few
# thousand files takes a while, and a commit per file would spend it on fsync;
# one commit at the end would throw the whole pass away on Ctrl-C. This keeps
# what has been checked, in chunks small enough that interrupting costs seconds.
_COMMIT_EVERY = 50


class ReconcileResult(BaseModel):
    """What a reconcile pass found.

    Attributes
    ----------
    checked : int
        Files the sync was about to send, each of which was looked up on the
        server.
    adopted : int
        Files whose stored copy matched byte for byte, now recorded as sent.
    differs : int
        Files the server holds under a different content. They stay on the list
        to send.
    absent : int
        Files the server does not hold at all. They stay on the list to send.
    errors : list[str]
        Failures, one line each. A file that could not be checked is left
        alone, so a failed check costs an upload and never a skip.
    dry_run : bool
        Whether the state was left untouched.
    """

    checked: int = 0
    adopted: int = 0
    differs: int = 0
    absent: int = 0
    errors: list[str] = Field(default_factory=list)
    dry_run: bool = False

    @property
    def still_to_send(self) -> int:
        """Files that must still be uploaded once this pass is done.

        A file whose check failed is counted here, because that is where it
        ends up: nothing was recorded for it, so the next run sends it. Leaving
        it out would report a number the next run contradicts.
        """
        return self.differs + self.absent + len(self.errors)


def server_inventory(
    client: OvClient, root_uri: str, *, timeout: float | None = None
) -> dict[str, ServerFile]:
    """Return everything stored under a root, keyed by path below it.

    One request for the whole tree, rather than a stat per file: the point of
    reconciling is to be cheaper than the upload it replaces, and several
    thousand round trips is not.

    Parameters
    ----------
    client :
        Connected OpenViking client.
    root_uri :
        The sync root.
    timeout :
        Read timeout for the listing, in seconds.

    Returns
    -------
    dict[str, ServerFile]
        Each stored file, keyed by its path below ``root_uri``.
    """
    return {entry.rel_path: entry for entry in client.tree(root_uri, timeout=timeout)}


def reconcile(
    folder: Path,
    root_uri: str,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    candidates: Collection[LocalFile],
    *,
    dry_run: bool = False,
    on_progress: ProgressCallback | None = None,
) -> ReconcileResult:
    """Record the candidates OpenViking already holds, and send nothing.

    Parameters
    ----------
    folder :
        Root of the synced folder.
    root_uri :
        Target directory URI.
    client :
        Connected OpenViking client.
    sync_config :
        Scan rules and timeouts.
    state :
        The folder's sync state, already open.
    candidates :
        The files a sync would send. Passing them in rather than planning
        again keeps one set of rules about what may be sent at all: a file the
        sync refuses to write — because its name clashes, or it would bury
        another — must not be adopted either.
    dry_run :
        Report what would be adopted without writing any of it down.
    on_progress :
        Called as the listing and comparison advance.

    Returns
    -------
    ReconcileResult
        What matched, what did not, and what could not be checked.

    Raises
    ------
    RootUriMismatch
        If this folder was previously synced to a different root.
    OvError
        If the server's listing cannot be read. Without it every file would
        look absent, and the pass would report a clean bill for a server it
        never managed to ask.
    """
    state.check_root(root_uri)
    if not dry_run:
        # Every row this pass writes carries a URI built from this root, so the
        # root has to be recorded with them. Leaving it unset would let a later
        # run be pointed at a different root without the mismatch guard firing:
        # it has nothing to compare against, the adopted digests still match, so
        # every file reads as already-synced and not one byte reaches the new
        # root. A dry run records nothing, and so must not bind anything either.
        state.bind_root(root_uri)

    if on_progress:
        on_progress("listing", 0, 0, root_uri)
    stored = server_inventory(
        client, root_uri, timeout=sync_config.request_timeout_seconds
    )
    if on_progress:
        on_progress("listing", len(stored), len(stored), f"{len(stored)} stored")

    entries = list(candidates)
    result = ReconcileResult(dry_run=dry_run)
    adopted: list[tuple[LocalFile, str, str]] = []

    def commit() -> None:
        """Write the adoptions gathered so far, unless this is a dry run."""
        if adopted and not dry_run:
            state.mark_synced(adopted)
        adopted.clear()

    # The commit is in a finally rather than after the loop so that a Ctrl-C
    # keeps what was already verified. Every row in there was settled by a hash
    # that matched, and throwing them away would make the interrupted pass cost
    # more than it saved.
    try:
        for index, entry in enumerate(entries):
            if on_progress:
                on_progress("checking", index, len(entries), entry.relative_path)
            result.checked += 1

            uri_path = safe_relative_path(entry.relative_path)
            held = stored.get(uri_path)
            if held is None:
                result.absent += 1
                continue

            try:
                local, read = read_stable(entry)
            except OSError as exc:
                result.errors.append(f"{entry.relative_path}: {exc}")
                continue

            # Checked before the download, not to save bytes but to save the
            # round trip: a file whose stored size differs cannot hash the
            # same, and at several thousand files the requests are the cost.
            if held.size != len(local):
                result.differs += 1
                continue

            uri = target_uri(root_uri, entry.relative_path)
            try:
                remote = client.download(uri, timeout=sync_config.request_timeout_seconds)
            except OvError as exc:
                result.errors.append(f"{entry.relative_path}: {exc}")
                continue

            if remote is None:
                # Listed a moment ago and gone now, or never really there.
                # Either way the sync should send it.
                result.absent += 1
                continue

            digest = hashlib.sha256(local).hexdigest()
            if hashlib.sha256(remote).hexdigest() != digest:
                result.differs += 1
                continue

            result.adopted += 1
            adopted.append((read, uri, digest))
            if len(adopted) >= _COMMIT_EVERY:
                commit()
    finally:
        commit()
    if on_progress:
        on_progress("checking", len(entries), len(entries), f"{result.adopted} adopted")
    return result
