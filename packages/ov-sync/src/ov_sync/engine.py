"""Work out what changed, send it, and record what landed.

The order matters. Everything that reads — scanning, hashing, diffing — happens
before anything writes, so a dry run and a real run see the same picture, and a
failure part-way leaves the state file describing exactly what reached the
server.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from .client import BatchWriteOperation, OvClient, OvError
from .config import SERVER_MAX_FILE_BYTES, SERVER_MAX_TOTAL_BYTES, SyncConfig
from .scanner import LocalFile, file_digest, scan
from .state import SyncedFile, SyncState, diff
from .uri_path import UriPathMap, folder_prefixes, map_uri_paths, safe_relative_path

# Called as (phase, done, total, detail). Phases are "scanning", "hashing",
# "writing", "deleting" and "done".
ProgressCallback = Callable[[str, int, int, str], None]


class SkippedFile(BaseModel):
    """A file left out of the sync, and why."""

    relative_path: str = Field(description="Path relative to the synced folder.")
    reason: str = Field(description="Why it was skipped.")


class RenamedFile(BaseModel):
    """A file whose name had to change to survive as a URI."""

    relative_path: str = Field(description="Path relative to the synced folder.")
    uri_path: str = Field(description="Path it lands under, inside the root URI.")


class TakenOverFile(BaseModel):
    """A synced copy another file is about to replace.

    Only a rewrite can produce this: it makes two local paths share one URI,
    so a file can inherit the place an earlier one was sent to. Replacing it
    is right — the URI belongs to whoever owns the name — but doing it without
    a word would lose a file's stored copy silently.
    """

    relative_path: str = Field(description="The file whose copy is replaced.")
    taken_by: str = Field(description="The file replacing it.")
    uri: str = Field(description="The URI they share.")


class SyncPlan(BaseModel):
    """What a run would do, without doing it.

    Attributes
    ----------
    write : list[str]
        Relative paths that would be sent.
    delete : list[str]
        URIs whose local file is gone.
    unchanged : int
        Files whose size and mtime match the state, so they were never read.
    touched : int
        Files whose mtime moved but whose contents did not. They cost a hash
        and nothing else.
    deferred : int
        Changed files this run was told to leave alone, because ``only``
        narrowed it to the paths an event named. Watch mode is the only
        caller that sets it; a full run leaves it at zero.
    skipped : list[SkippedFile]
        Files that cannot be sent, with the reason.
    renamed : list[RenamedFile]
        Files in ``write`` that land under a different name than they have on
        disk, because their own carries something a URI cannot. Only the files
        being sent are listed: a rename is news when the file goes, and
        repeating it on every idle run would bury the lines that matter.
    taken_over : list[TakenOverFile]
        Copies in OpenViking that this run replaces with another file's
        contents, because a rewrite had the two share a URI.
    """

    write: list[str] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)
    unchanged: int = 0
    touched: int = 0
    deferred: int = 0
    skipped: list[SkippedFile] = Field(default_factory=list)
    renamed: list[RenamedFile] = Field(default_factory=list)
    taken_over: list[TakenOverFile] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to write and nothing to delete."""
        return not self.write and not self.delete


class SyncResult(BaseModel):
    """What a run actually did.

    Attributes
    ----------
    scanned : int
        Files found in the folder, after exclusions.
    created : int
        Files that did not exist in OpenViking before.
    updated : int
        Files whose content was replaced.
    unchanged : int
        Files the sync did not need to touch at all.
    touched : int
        Files whose mtime moved without their contents changing.
    deferred : int
        Changed files left for a later run, because ``only`` narrowed this one.
    deleted_detected : list[str]
        URIs whose local file is gone. Reported whether or not they were
        removed, so a run without ``--delete`` still says what it saw.
    deleted_applied : int
        How many of those were actually removed.
    snapshot_id : str or None
        The snapshot committed before deleting, when one was taken.
    skipped : list[SkippedFile]
        Files left out, with the reason.
    errors : list[str]
        Failures, one line each. A run with errors is not a lost run: the
        batches that did land are recorded, and the rest retry next time.
    plan : SyncPlan
        What the run set out to do. On a dry run this is the whole answer.
    dry_run : bool
        Whether anything was actually sent.
    """

    scanned: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    touched: int = 0
    deferred: int = 0
    deleted_detected: list[str] = Field(default_factory=list)
    deleted_applied: int = 0
    snapshot_id: str | None = None
    skipped: list[SkippedFile] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    plan: SyncPlan = Field(default_factory=SyncPlan)
    dry_run: bool = False

    @property
    def written(self) -> int:
        """Files sent to the server this run."""
        return self.created + self.updated


@dataclass(frozen=True)
class Superseded:
    """A state row whose URI another file has taken over.

    Attributes
    ----------
    row : SyncedFile
        The old row. Its own file is gone from the folder, or held back by a
        clash, so the row no longer describes anything ov-sync tracks.
    taken_by : str
        Relative path of the file that occupies that URI now.
    """

    row: SyncedFile
    taken_by: str


@dataclass(frozen=True)
class PlanDetail:
    """The files behind a :class:`SyncPlan`, for the code that acts on it.

    Attributes
    ----------
    to_write : list[LocalFile]
        Files to send, in the order they will be batched.
    touched : list[LocalFile]
        Files whose mtime moved but whose contents match what was last sent.
        Their new mtime is worth recording so the next run skips the hash.
    deleted : list[SyncedFile]
        State rows whose file is gone from disk, and whose URI no other file
        has taken over.
    superseded : list[Superseded]
        State rows describing a URI another file now occupies. They are
        dropped from the state rather than acted on: removing the URI instead
        would delete the file that took it over.
    scanned : int
        How many files the scan found.
    """

    to_write: list[LocalFile] = field(default_factory=list)
    touched: list[LocalFile] = field(default_factory=list)
    deleted: list[SyncedFile] = field(default_factory=list)
    superseded: list[Superseded] = field(default_factory=list)
    scanned: int = 0


def target_uri(root_uri: str, relative_path: str) -> str:
    """Return where a file lands in OpenViking.

    The mapping is positional and total: a file's path under the folder is its
    path under the root. That is what makes the sync idempotent without a
    server-side key — the URI *is* the key. Positional does not mean literal:
    a name carrying something a URI cannot hold is rewritten on the way
    through, by the one rule in :mod:`ov_sync.uri_path`, so the same file
    always lands on the same URI.

    Parameters
    ----------
    root_uri :
        The target root, e.g. ``viking://resources/notes``.
    relative_path :
        The file's path relative to the synced folder, forward-slashed.

    Returns
    -------
    str
        The target URI.
    """
    return f"{root_uri.rstrip('/')}/{safe_relative_path(relative_path)}"


def build_operation(entry: LocalFile, uri: str) -> tuple[BatchWriteOperation, str, int]:
    """Read a file and wrap it as a batch-write operation.

    Text goes as text so the server stores it verbatim; anything that is not
    valid UTF-8 goes base64-encoded.

    Parameters
    ----------
    entry :
        The file to read.
    uri :
        Where it should land.

    Returns
    -------
    tuple[BatchWriteOperation, str, int]
        An upsert for that URI, the SHA-256 of the bytes it carries, and how
        many bytes those are. The digest comes from the bytes actually sent,
        not from a second read, so the state can never record a hash of
        content the server never saw; the count is what the batching measures,
        because the size the scan recorded may already be stale.
    """
    data = entry.path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return (
            BatchWriteOperation(
                uri=uri,
                content_base64=base64.b64encode(data).decode("ascii"),
                mode="upsert",
            ),
            digest,
            len(data),
        )
    return BatchWriteOperation(uri=uri, content=text, mode="upsert"), digest, len(data)


def plan_sync(
    folder: Path,
    sync_config: SyncConfig,
    state: SyncState,
    *,
    full: bool = False,
    only: Collection[str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[SyncPlan, PlanDetail]:
    """Decide what needs sending, without sending it.

    Files whose size and mtime are unchanged are never opened. The rest are
    hashed, and a file whose hash matches what was last sent is not re-sent —
    a touched mtime is not an edit, and every write costs a re-embed on the
    server.

    A file whose name has to be rewritten to fit a URI is still sent, under the
    new name. One that would then land on a name another file has a better
    claim to is not: it is skipped, with the clash spelled out.

    Parameters
    ----------
    folder :
        Root of the synced folder.
    sync_config :
        Scan rules and batch limits.
    state :
        The folder's sync state.
    full :
        Ignore the state and re-send everything.
    only :
        Limit writes to these relative paths. Deletions are still detected, so
        a watch-mode run still reports them.
    on_progress :
        Called as the scan and hash phases advance.

    Returns
    -------
    tuple[SyncPlan, PlanDetail]
        The plan, and the files behind it.
    """
    if on_progress:
        on_progress("scanning", 0, 0, str(folder))
    files = scan(folder, sync_config)
    if on_progress:
        on_progress("scanning", len(files), len(files), f"{len(files)} files")

    tracked = state.all_files()
    state_diff = diff(state, files)
    to_hash = list(files) if full else state_diff.added + state_diff.candidates

    # Before anything is read: two files can want one URI once one of them is
    # rewritten, and sending both would have the second quietly overwrite the
    # first. Held back here rather than at the write, so a dry run reports the
    # clash and no file is hashed only to be dropped.
    uri_paths = map_uri_paths(entry.relative_path for entry in files)
    superseded, deleted = _split_superseded(tracked, state_diff.deleted, uri_paths)
    blocked = {
        **uri_paths.blocked,
        **_block_stale_nodes(
            uri_paths.safe, tracked, {entry.relative_path for entry in files}
        ),
    }
    skipped: list[SkippedFile] = [
        SkippedFile(relative_path=relative_path, reason=reason)
        for relative_path, reason in sorted(blocked.items())
    ]
    to_hash = [entry for entry in to_hash if entry.relative_path not in blocked]

    deferred = 0
    if only is not None:
        allowed = set(only)
        kept = [entry for entry in to_hash if entry.relative_path in allowed]
        deferred = len(to_hash) - len(kept)
        to_hash = kept

    to_write: list[LocalFile] = []
    touched: list[LocalFile] = []
    for index, entry in enumerate(to_hash):
        if on_progress:
            on_progress("hashing", index, len(to_hash), entry.relative_path)
        if entry.size > SERVER_MAX_FILE_BYTES:
            skipped.append(
                SkippedFile(
                    relative_path=entry.relative_path,
                    reason=f"{entry.size} bytes is over the server's "
                    f"{SERVER_MAX_FILE_BYTES}-byte limit for a single file",
                )
            )
            continue
        try:
            digest = file_digest(entry.path)
        except OSError as exc:
            skipped.append(
                SkippedFile(
                    relative_path=entry.relative_path, reason=f"unreadable: {exc}"
                )
            )
            continue
        previous = tracked.get(entry.relative_path)
        if previous is not None and previous.digest == digest and not full:
            touched.append(entry)
        else:
            to_write.append(entry)
    if on_progress:
        on_progress("hashing", len(to_hash), len(to_hash), f"{len(to_write)} to send")

    plan = SyncPlan(
        write=[entry.relative_path for entry in to_write],
        delete=[row.uri for row in deleted],
        # Counted from the diff, not by subtraction. A file `only` held back
        # is deferred, not unchanged, and reporting it as unchanged would tell
        # the user the opposite of the truth about a file that did change.
        #
        # A full run has no unchanged files by definition: it re-sends every
        # one, including the ones the size-and-mtime diff called unchanged.
        # Counting them here as well would report each file twice.
        # A blocked file is skipped, not unchanged. Counting it in both would
        # report the same file as settled and as outstanding in one breath.
        unchanged=0
        if full
        else sum(
            1 for entry in state_diff.unchanged if entry.relative_path not in blocked
        ),
        touched=len(touched),
        deferred=deferred,
        skipped=skipped,
        renamed=[
            RenamedFile(
                relative_path=entry.relative_path,
                uri_path=uri_paths.rewritten[entry.relative_path],
            )
            for entry in to_write
            if entry.relative_path in uri_paths.rewritten
        ],
        # Only what this run actually sends: a takeover the operator has been
        # told about once, and which the write then records, is not news twice.
        taken_over=[
            TakenOverFile(
                relative_path=item.row.relative_path,
                taken_by=item.taken_by,
                uri=item.row.uri,
            )
            for item in superseded
            if item.taken_by in {entry.relative_path for entry in to_write}
        ],
    )
    detail = PlanDetail(
        to_write=to_write,
        touched=touched,
        deleted=deleted,
        superseded=superseded,
        scanned=len(files),
    )
    return plan, detail


def _block_stale_nodes(
    safe: dict[str, str],
    tracked: dict[str, SyncedFile],
    present: Collection[str],
) -> dict[str, str]:
    """Hold back files that would land under a file already in OpenViking.

    Sync ``notes.md``, delete it from the folder, then make a folder called
    ``notes.md`` and put a file in it. The old file is still in OpenViking at
    that URI — a deletion is only reported until ``--delete`` acts on it — and
    writing underneath it buries the new file: the server takes the write and
    reports success, but ``ls`` on that node returns nothing from then on, so
    everything under it is invisible to listing, and no amount of further
    writing digs it out. Removing the file and *then* writing beneath the node
    does, which is what ``--delete`` and the run after it amount to — so the
    way out is the one the message names, not a second attempt.

    Every state row records a file this sync put at a URI, so every one of them
    is a node nothing may be written beneath. Reading the rows rather than this
    run's scan is the point: the file that is in the way is one an *earlier*
    run sent, and it is in the way whether or not it is still on disk.

    This one is not the rewrite's doing — a folder replacing a file of the same
    name does it just as well, and has since the first version — but the
    rewrite makes more names meet, and the check belongs where the URIs are
    decided.

    Parameters
    ----------
    safe :
        Where each file on disk lands, from :func:`map_uri_paths`.
    tracked :
        Every state row, keyed by relative path.
    present :
        The relative paths the scan found, to tell a file that is merely in the
        way from one the operator has already deleted.

    Returns
    -------
    dict[str, str]
        Relative path to the reason it has to wait.
    """
    nodes = {safe_relative_path(path): row for path, row in tracked.items()}
    if not nodes:
        return {}

    blocked: dict[str, str] = {}
    for relative_path, uri_path in safe.items():
        for parent in folder_prefixes(uri_path):
            # Never this file's own row: a path is not one of its own parents.
            row = nodes.get(parent)
            if row is None:
                continue
            # Renaming the file in the way does not clear it on its own: the
            # copy an earlier run sent stays at that URI until a delete takes
            # it, so the message asks for both steps.
            remedy = (
                "Rename one of them, then re-run with --delete to clear the "
                "copy left behind."
                if row.relative_path in present
                else "Re-run with --delete to remove it first."
            )
            blocked[relative_path] = (
                f"{parent!r} in OpenViking is the file synced from "
                f"{row.relative_path!r}. Writing underneath it would hide this "
                f"file from listing. {remedy}"
            )
            break
    return blocked


def _split_superseded(
    tracked: dict[str, SyncedFile],
    deleted: Sequence[SyncedFile],
    uri_paths: UriPathMap,
) -> tuple[list[Superseded], list[SyncedFile]]:
    """Separate rows whose URI another file has taken over from real deletions.

    Rewriting a name makes the mapping from path to URI many-to-one, and that
    lets one file inherit the URI another file's row still names. Rename
    ``a#b.md`` to ``a_b.md`` — which is exactly what a clash message asks for —
    and both paths point at ``a_b.md``. Treating the old row as a deletion
    would remove the file that was just written under it, leaving the state
    claiming a file is synced that is no longer there.

    Parameters
    ----------
    tracked :
        Every state row, keyed by relative path.
    deleted :
        The rows the diff called deleted, in its order.
    uri_paths :
        Where each file on disk lands now.

    Returns
    -------
    tuple[list[Superseded], list[SyncedFile]]
        The superseded rows, each with the file that took its URI, and the
        deletions that remain.
    """
    owners = {uri_path: path for path, uri_path in uri_paths.safe.items()}
    superseded = [
        Superseded(row=row, taken_by=owners[safe_relative_path(path)])
        for path, row in sorted(tracked.items())
        # A row whose file is still going keeps its URI by definition; only a
        # file that is gone from disk, or held back by a clash, can be
        # displaced by another.
        if path not in uri_paths.safe and safe_relative_path(path) in owners
    ]
    displaced = {item.row.relative_path for item in superseded}
    return superseded, [row for row in deleted if row.relative_path not in displaced]


def sync(
    folder: Path,
    root_uri: str,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    *,
    full: bool = False,
    dry_run: bool = False,
    apply_deletes: bool = False,
    snapshot_before_delete: bool = True,
    only: Collection[str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> SyncResult:
    """Sync a folder into OpenViking.

    Parameters
    ----------
    folder :
        Root of the synced folder.
    root_uri :
        Target directory URI. It must already exist.
    client :
        Connected OpenViking client.
    sync_config :
        Scan rules and batch limits.
    state :
        The folder's sync state, already open.
    full :
        Ignore the state and re-send everything. Safe at any time: every write
        is an upsert.
    dry_run :
        Work out the plan and return it without sending anything.
    apply_deletes :
        Remove resources whose local file is gone. Off by default, so a
        deletion is reported before it is acted on.
    snapshot_before_delete :
        Commit a snapshot before removing anything, so the removal can be
        undone with ``ov snapshot restore``. When the snapshot fails, the
        deletions are abandoned rather than done without a safety net.
    only :
        Limit writes to these relative paths.
    on_progress :
        Called as each phase advances.

    Returns
    -------
    SyncResult
        Counts, the plan, and any errors.

    Raises
    ------
    RootUriMismatch
        If this folder was previously synced to a different root.
    """
    # Checked on every run, recorded only on a real one. A dry run that wrote
    # the root down would commit the folder to a URI on the strength of a
    # command whose whole promise is that it changes nothing -- and a typo in
    # that URI could then only be undone by deleting the state file.
    state.check_root(root_uri)
    plan, detail = plan_sync(
        folder,
        sync_config,
        state,
        full=full,
        only=only,
        on_progress=on_progress,
    )

    result = SyncResult(
        scanned=detail.scanned,
        unchanged=plan.unchanged,
        touched=plan.touched,
        deferred=plan.deferred,
        deleted_detected=plan.delete,
        skipped=list(plan.skipped),
        plan=plan,
        dry_run=dry_run,
    )
    if dry_run:
        return result

    state.bind_root(root_uri)

    # A touched mtime is worth recording even when nothing else happens:
    # without it, the next run hashes the same unchanged file all over again.
    state.touch(detail.touched)

    _write_batches(
        client, root_uri, detail.to_write, sync_config, state, result, on_progress
    )

    # Dropped only once some row names that URI: the file that took it over,
    # whether it landed just now or in an earlier run. Dropping the old row
    # before the write would strand the old copy when the write fails --
    # nothing would name the URI any more, so a later --delete could never
    # reach it. A row left behind costs nothing: it is kept out of the
    # deletions for as long as the file holding that URI is there.
    landed = state.all_files()
    state.forget(
        [item.row.relative_path for item in detail.superseded if item.taken_by in landed]
    )

    if detail.deleted and apply_deletes:
        _apply_deletes(
            client,
            root_uri,
            detail.deleted,
            state,
            result,
            snapshot_before_delete=snapshot_before_delete,
            on_progress=on_progress,
        )

    if on_progress:
        on_progress("done", result.written, len(plan.write), _summary(result))
    return result


def _write_batches(
    client: OvClient,
    root_uri: str,
    to_write: Sequence[LocalFile],
    sync_config: SyncConfig,
    state: SyncState,
    result: SyncResult,
    on_progress: ProgressCallback | None,
) -> None:
    """Send the files, one batch per request, recording what each batch landed.

    Batches are filled by the bytes actually read, not by the sizes the scan
    recorded. A file can grow between the two — a download finishing, a log
    rolling — and a batch sized on the stale number is refused by the server
    as a whole, taking every other file in it down with one that changed.

    A failed batch is left out of the state entirely, even though the server
    may have written some of its files before it stopped: it does not report
    which. Re-sending a file that already landed costs nothing — the write is
    an upsert — so the conservative choice risks a repeat, never a loss.
    """
    done = 0
    operations: list[BatchWriteOperation] = []
    pending: list[tuple[LocalFile, str, str]] = []
    batch_bytes = 0

    def flush() -> None:
        """Send what has accumulated, and record it if the server took it."""
        nonlocal operations, pending, batch_bytes
        if not operations:
            return
        try:
            written = client.batch_write(
                root_uri,
                operations,
                wait=sync_config.wait_for_indexing,
                timeout=sync_config.request_timeout_seconds,
            )
        except OvError as exc:
            result.errors.append(f"batch of {len(operations)} file(s) failed: {exc}")
        else:
            result.created += len(written.created)
            result.updated += len(written.updated)
            state.mark_synced(pending)
        operations = []
        pending = []
        batch_bytes = 0

    for entry in to_write:
        if on_progress:
            on_progress("writing", done, len(to_write), entry.relative_path)
        done += 1
        uri = target_uri(root_uri, entry.relative_path)
        try:
            operation, digest, size = build_operation(entry, uri)
        except OSError as exc:
            result.skipped.append(
                SkippedFile(
                    relative_path=entry.relative_path, reason=f"unreadable: {exc}"
                )
            )
            continue

        if size > SERVER_MAX_FILE_BYTES:
            result.skipped.append(
                SkippedFile(
                    relative_path=entry.relative_path,
                    reason=f"grew to {size} bytes while the sync ran, over the "
                    f"server's {SERVER_MAX_FILE_BYTES}-byte limit for a single file",
                )
            )
            continue

        at_limit = len(operations) >= sync_config.max_operations_per_batch
        would_overflow = batch_bytes + size > SERVER_MAX_TOTAL_BYTES
        if operations and (at_limit or would_overflow):
            flush()

        operations.append(operation)
        pending.append((entry, uri, digest))
        batch_bytes += size

    flush()
    if on_progress:
        on_progress("writing", done, len(to_write), f"{done}/{len(to_write)}")


def _apply_deletes(
    client: OvClient,
    root_uri: str,
    deleted: Sequence[SyncedFile],
    state: SyncState,
    result: SyncResult,
    *,
    snapshot_before_delete: bool,
    on_progress: ProgressCallback | None,
) -> None:
    """Remove resources whose local file is gone, after taking a snapshot.

    OpenViking has no archive state, so the only way to take something out of
    retrieval is to remove it. The snapshot is the undo: it is committed before
    the first removal, and if it fails nothing is removed.
    """
    if snapshot_before_delete:
        try:
            snapshot = client.snapshot_commit(
                f"ov-sync: before removing {len(deleted)} file(s) under {root_uri}",
                paths=[root_uri],
            )
            result.snapshot_id = snapshot.commit_id
        except OvError as exc:
            result.errors.append(
                f"snapshot failed, so nothing was deleted: {exc}. "
                "Re-run with --no-snapshot to delete without one."
            )
            return

    removed: list[str] = []
    for index, row in enumerate(deleted):
        if on_progress:
            on_progress("deleting", index, len(deleted), row.relative_path)
        try:
            client.rm(row.uri)
        except OvError as exc:
            result.errors.append(f"rm {row.uri}: {exc}")
            continue
        removed.append(row.relative_path)

    result.deleted_applied = len(removed)
    state.forget(removed)


def _summary(result: SyncResult) -> str:
    """One line describing what a run did, for the progress display."""
    parts = []
    if result.created:
        parts.append(f"{result.created} created")
    if result.updated:
        parts.append(f"{result.updated} updated")
    if result.deleted_applied:
        parts.append(f"{result.deleted_applied} deleted")
    if result.errors:
        parts.append(f"{len(result.errors)} failed")
    return ", ".join(parts) or "nothing to do"
