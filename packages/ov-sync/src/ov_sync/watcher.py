"""Keep syncing a folder as it changes.

Two strategies. Event mode reacts to filesystem notifications and debounces
them, so a burst of editor saves becomes one sync. Poll mode rescans on a
timer, which is slower to react but works where notifications do not — network
mounts, some containers.

Event mode never trusts events alone. It syncs the whole folder on startup, and
again on a timer, because a file edited while the watcher was stopped produced
no event and watchdog silently drops events when its queue overflows. Without
that, one missed event means a file that is never synced again.

Everything that touches the state database runs on the calling thread. The
watchdog handler only records path names and sets an event, because a SQLite
connection belongs to the thread that opened it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .client import OvClient
from .config import OvSyncConfig
from .engine import SyncResult, sync
from .scanner import is_excluded, is_syncable
from .state import SyncState

SyncReporter = Callable[[SyncResult], None]
ErrorReporter = Callable[[Exception], None]

# How many debounce periods the quiet-period wait may be extended to before it
# gives up and syncs anyway. Five minutes at the shipped 5-second default:
# long enough to sit out a checkout, short enough that a folder being written
# to continuously still gets synced.
MAX_SETTLE_PERIODS = 60


@dataclass(frozen=True)
class Pass:
    """What the event loop should do on one turn.

    Attributes
    ----------
    should_sync : bool
        Whether to sync at all. False only when nothing happened and no
        rescan is owed.
    scope : set[str] or None
        Paths to write, or None to consider the whole folder.
    resets_reconcile : bool
        Whether this pass discharges the periodic rescan.
    """

    should_sync: bool
    scope: set[str] | None
    resets_reconcile: bool


def plan_pass(*, woken: bool, drained: set[str] | None, overdue: bool) -> Pass:
    """Decide what one turn of the event loop should sync.

    Pulled out of the loop because the loop is hard to test and this is not:
    two of the three ways this can be wrong shipped once already, and both
    were timing-dependent enough that a test driving the real loop missed them.

    Parameters
    ----------
    woken :
        Whether an event arrived this turn.
    drained :
        What the collector handed over: the paths seen, or None when it wants
        the whole folder looked at.
    overdue :
        Whether the periodic rescan is due.

    Returns
    -------
    Pass
        Whether to sync, over what, and whether that discharges the rescan.
    """
    if overdue:
        # Regardless of `woken`. Events arrive exactly when watchdog is most
        # likely to be dropping them, so gating the rescan on an idle folder
        # would skip it whenever it was actually needed.
        return Pass(should_sync=True, scope=None, resets_reconcile=True)
    if not woken:
        return Pass(should_sync=False, scope=None, resets_reconcile=False)
    # A pass that looked at everything counts as a reconcile; a partial one
    # does not. Crediting a partial sync would push the deadline out on every
    # save, so an ordinary editing session would never get a rescan.
    return Pass(should_sync=True, scope=drained, resets_reconcile=drained is None)


class _ChangeCollector(FileSystemEventHandler):
    """Collects the relative paths of files that changed.

    Parameters
    ----------
    folder :
        Root of the watched folder, already resolved.
    config :
        Supplies the scan rules, so an ignored path does not wake the sync.
    wakeup :
        Set whenever a relevant change arrives.

    Attributes
    ----------
    full_rescan : bool
        Set when something happened that no path name can describe — a
        deletion, or a directory being moved. The next sync then considers
        every file instead of the ones named.
    """

    def __init__(
        self, folder: Path, config: OvSyncConfig, wakeup: threading.Event
    ) -> None:
        super().__init__()
        self._folder = folder
        self._config = config
        self._wakeup = wakeup
        self._lock = threading.Lock()
        self._changed: set[str] = set()
        self.full_rescan = False

    def _relative(self, raw_path: str | bytes) -> str | None:
        """Return an event's path relative to the watched folder, or None."""
        path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        try:
            return path.relative_to(self._folder).as_posix()
        except ValueError:
            return None

    def _record(self, raw_path: str | bytes) -> None:
        """Note a changed file, if it is one this sync would send.

        Filtering here is what stops ov-sync waking itself up: SQLite creates
        and deletes ``.ov-sync.db-journal`` on every commit, inside the very
        folder being watched.
        """
        relative_path = self._relative(raw_path)
        if relative_path is None or not is_syncable(relative_path, self._config.sync):
            return
        with self._lock:
            self._changed.add(relative_path)
        self._wakeup.set()

    def _request_rescan(self, raw_path: str | bytes, *, is_dir: bool = False) -> None:
        """Ask for a whole-folder sync, unless the path is one to ignore.

        A directory is judged on the exclusion rules alone. The extension
        filter cannot apply to it — a folder has no extension, and running it
        through the file test would silently drop every directory event,
        including the moves and deletions only a rescan can resolve.
        """
        relative_path = self._relative(raw_path)
        if relative_path is None:
            return
        if is_dir:
            if is_excluded(relative_path, self._config.sync.exclude, is_dir=True):
                return
        elif not is_syncable(relative_path, self._config.sync):
            return
        with self._lock:
            self.full_rescan = True
        self._wakeup.set()

    def drain(self) -> set[str] | None:
        """Take the accumulated paths and reset the collector.

        Returns
        -------
        set[str] or None
            The paths seen since the last drain, or None when the next sync
            has to look at everything.
        """
        with self._lock:
            paths = self._changed
            rescan = self.full_rescan
            self._changed = set()
            self.full_rescan = False
        return None if rescan else paths

    def on_created(self, event: FileSystemEvent) -> None:
        """Record a newly created file."""
        if not event.is_directory:
            self._record(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        """Record an edited file."""
        if not event.is_directory:
            self._record(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        """Record where a rename landed, and rescan for where it left.

        A directory move relocates every file under it at once, and the event
        names only the directory, so only a rescan finds them.
        """
        if event.is_directory:
            self._request_rescan(event.src_path, is_dir=True)
            return
        self._record(getattr(event, "dest_path", "") or event.src_path)
        self._request_rescan(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Rescan, so the deletion is noticed.

        The path is not recorded: a deleted file cannot be written, and it is
        the scan finding it absent that turns it into a reported deletion.
        """
        self._request_rescan(event.src_path, is_dir=event.is_directory)


class FolderWatcher:
    """Syncs a folder whenever it changes, until stopped.

    Parameters
    ----------
    folder :
        Root of the folder to watch.
    root_uri :
        Where it lands in OpenViking.
    client :
        Connected OpenViking client.
    config :
        Sync and watch settings.
    apply_deletes :
        Remove resources whose local file is gone. Off by default: a watch
        loop deleting things unattended is a bad surprise.
    on_sync :
        Called with the result of every sync.
    on_error :
        Called when a sync raises. Without it the exception propagates and
        ends the watch, which is the right default for a library caller and
        the wrong one for a long-running command.
    """

    def __init__(
        self,
        folder: Path,
        root_uri: str,
        client: OvClient,
        config: OvSyncConfig,
        *,
        apply_deletes: bool = False,
        on_sync: SyncReporter | None = None,
        on_error: ErrorReporter | None = None,
    ) -> None:
        self._folder = folder.resolve()
        self._root_uri = root_uri
        self._client = client
        self._config = config
        self._apply_deletes = apply_deletes
        self._on_sync = on_sync
        self._on_error = on_error
        self._stop = threading.Event()

    def stop(self) -> None:
        """Ask the watcher to finish the current cycle and return."""
        self._stop.set()

    def run(self) -> None:
        """Watch until :meth:`stop` is called.

        Raises
        ------
        ValueError
            If the configured watch mode is not recognized.
        """
        mode = self._config.watch.mode
        if mode == "events":
            self._run_events()
        elif mode == "poll":
            self._run_poll()
        else:  # pragma: no cover -- the config validator rejects anything else
            raise ValueError(f"Unknown watch mode: {mode}")

    def sync_once(self, only: set[str] | None = None) -> SyncResult:
        """Run one sync, opening and closing the state around it.

        Parameters
        ----------
        only :
            Limit writes to these paths. None considers the whole folder.

        Returns
        -------
        SyncResult
            What the sync did.
        """
        db_path = self._folder / self._config.sync.state_file
        with SyncState(db_path) as state:
            result = sync(
                self._folder,
                self._root_uri,
                self._client,
                self._config.sync,
                state,
                apply_deletes=self._apply_deletes,
                only=only,
            )
        if self._on_sync is not None:
            self._on_sync(result)
        return result

    def _sync_guarded(self, only: set[str] | None = None) -> None:
        """Run one sync, reporting a failure instead of ending the watch.

        ``sync`` turns a server error into ``result.errors``, but the scan and
        the state database raise: an unmounted volume gives ``NotADirectoryError``,
        a locked database gives a ``sqlite3`` error. A long-running watch must
        survive a blip and try again on the next cycle rather than exit with a
        traceback.

        With no ``on_error`` handler the exception propagates, so a caller who
        did not ask for errors to be swallowed does not get them swallowed.
        """
        try:
            self.sync_once(only)
        except Exception as exc:
            if self._on_error is None:
                raise
            self._on_error(exc)

    def _run_events(self) -> None:
        """React to filesystem events, debounced, with a periodic rescan."""
        wakeup = threading.Event()
        collector = _ChangeCollector(self._folder, self._config, wakeup)
        observer = Observer()
        observer.schedule(collector, str(self._folder), recursive=True)
        observer.start()

        reconcile_every = self._config.watch.reconcile_interval_seconds
        try:
            # Anything edited while the watcher was down produced no event, so
            # events alone would never send it.
            self._sync_guarded()
            last_reconcile = time.monotonic()

            while not self._stop.is_set():
                woken = wakeup.wait(timeout=1.0)
                if woken:
                    wakeup.clear()
                    self._settle(wakeup)
                    if self._stop.is_set():
                        break

                drained = collector.drain() if woken else set[str]()
                overdue = time.monotonic() - last_reconcile >= reconcile_every
                pending = plan_pass(woken=woken, drained=drained, overdue=overdue)
                if not pending.should_sync:
                    continue

                self._sync_guarded(pending.scope)
                if pending.resets_reconcile:
                    last_reconcile = time.monotonic()
        finally:
            observer.stop()
            observer.join()

    def _settle(self, wakeup: threading.Event) -> None:
        """Wait for the folder to go quiet, up to a bounded maximum.

        A quiet period, not a fixed delay: each fresh event restarts the
        clock, so syncing partway through a bulk change — a checkout, a vault
        sync, a folder dragged to the trash — does not upload half-written
        files or, with --delete on, remove ones only momentarily absent.

        The bound is what stops that becoming a stall. Without it, anything
        writing to a synced file more often than the debounce — a tailing log,
        a plugin, a script — parks the loop here forever, and the folder is
        never synced again while the writes continue.
        """
        debounce = self._config.watch.debounce_seconds
        deadline = time.monotonic() + debounce * MAX_SETTLE_PERIODS
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if not wakeup.wait(timeout=min(debounce, remaining)):
                return
            wakeup.clear()

    def _run_poll(self) -> None:
        """Rescan on a timer."""
        interval = self._config.watch.poll_interval_seconds
        while not self._stop.is_set():
            self._sync_guarded()
            self._stop.wait(timeout=interval)
