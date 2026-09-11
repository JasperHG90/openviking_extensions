"""Tests for continuous sync.

The watcher is the only threaded code here, and threads are where the two
worst bugs in this package lived: it woke itself up forever on its own SQLite
journal, and in event mode it never reconciled, so anything edited while it was
stopped was never sent. Both are covered below.

These tests drive the collector and the loop directly rather than waiting on
real filesystem events, so they are fast and do not depend on the platform's
notification backend. `test_a_real_edit_reaches_the_server` is the one
end-to-end check that watchdog is wired up at all.

There is deliberately no "watch an idle folder and count the syncs" test. It
reads like the obvious guard against the feedback loop, but it only fires when
the platform delivers the journal event inside the window the test waits, so
it catches the bug on some runs and not others. The collector tests above
assert the same property with no clock in them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from helpers import write
from watchdog.events import (
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from ov_sync.client import OvClient
from ov_sync.config import Credentials, OvSyncConfig, SyncConfig, WatchConfig
from ov_sync.engine import SyncResult
from ov_sync.state import SyncState
from ov_sync.watcher import (
    MAX_SETTLE_PERIODS,
    FolderWatcher,
    _ChangeCollector,
    plan_pass,
)

BASE = "https://openviking.example/api/v1"
ROOT = "viking://resources/notes"


def ok(result: Any) -> httpx.Response:
    """Build a successful OpenViking response envelope."""
    return httpx.Response(200, json={"status": "ok", "result": result, "error": None})


def batch_reply(request: httpx.Request) -> httpx.Response:
    """Answer a batch-write by echoing every target back as created."""
    import json

    body = json.loads(request.content)
    return ok(
        {
            "root_uri": body["root_uri"],
            "created": [operation["uri"] for operation in body["operations"]],
            "updated": [],
            "unchanged": [],
        }
    )


@pytest.fixture
def client(credentials: Credentials) -> Iterator[OvClient]:
    """An OvClient pointed at the intercepted base URL."""
    with OvClient(credentials) as open_client:
        yield open_client


@pytest.fixture
def routes() -> Iterator[respx.MockRouter]:
    """Intercept the endpoints a watch-mode sync touches."""
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/content/batch-write", name="batch_write").mock(
            side_effect=batch_reply
        )
        mock.delete("/fs", name="rm").mock(return_value=ok({"uri": "x"}))
        mock.post("/snapshot/commit", name="snapshot").mock(
            return_value=ok({"commit_oid": "snap-1"})
        )
        yield mock


@pytest.fixture
def config() -> OvSyncConfig:
    """A watch configuration with the timers wound right down."""
    return OvSyncConfig(
        sync=SyncConfig(root_uri=ROOT),
        watch=WatchConfig(debounce_seconds=0.05, reconcile_interval_seconds=30.0),
    )


def collector(
    folder: Path, config: OvSyncConfig
) -> tuple[_ChangeCollector, threading.Event]:
    """Build a collector over ``folder`` and the event it sets."""
    wakeup = threading.Event()
    return _ChangeCollector(folder.resolve(), config, wakeup), wakeup


def test_the_sqlite_journal_never_wakes_the_watcher(
    folder: Path, config: OvSyncConfig
) -> None:
    """The state database lives in the watched folder, so its journal churns.

    SQLite creates `<db>-journal` and deletes it on every commit, and every
    sync commits. Before this filter the deletion woke a full sync, which
    committed, which deleted the journal again — a folder nobody was touching
    resynced every debounce period, forever.
    """
    handler, wakeup = collector(folder, config)
    journal = folder.resolve() / f"{config.sync.state_file}-journal"

    handler.on_created(FileCreatedEvent(str(journal)))
    handler.on_deleted(FileDeletedEvent(str(journal)))
    handler.on_modified(FileModifiedEvent(str(journal)))

    assert not wakeup.is_set()
    assert handler.drain() == set()


def test_an_excluded_path_never_wakes_the_watcher(
    folder: Path, config: OvSyncConfig
) -> None:
    """A git lockfile churns constantly and has nothing to do with the sync."""
    handler, wakeup = collector(folder, config)

    handler.on_created(FileCreatedEvent(str(folder.resolve() / ".git" / "index.lock")))
    handler.on_deleted(FileDeletedEvent(str(folder.resolve() / ".git" / "index.lock")))

    assert not wakeup.is_set()


def test_an_out_of_scope_extension_never_wakes_the_watcher(
    folder: Path, config: OvSyncConfig
) -> None:
    """A file the sync would not send is not worth waking for."""
    handler, wakeup = collector(folder, config)

    handler.on_modified(FileModifiedEvent(str(folder.resolve() / "notes.sqlite")))

    assert not wakeup.is_set()


def test_an_edit_is_collected(folder: Path, config: OvSyncConfig) -> None:
    """A relevant change names itself, so the sync can be narrowed to it."""
    handler, wakeup = collector(folder, config)

    handler.on_modified(FileModifiedEvent(str(folder.resolve() / "sub" / "note.md")))

    assert wakeup.is_set()
    assert handler.drain() == {"sub/note.md"}


def test_a_deletion_forces_a_full_rescan(folder: Path, config: OvSyncConfig) -> None:
    """A deleted file cannot be written; only the scan turns it into a removal."""
    handler, wakeup = collector(folder, config)

    handler.on_deleted(FileDeletedEvent(str(folder.resolve() / "note.md")))

    assert wakeup.is_set()
    assert handler.drain() is None


def test_a_rename_records_the_destination_and_rescans(
    folder: Path, config: OvSyncConfig
) -> None:
    """The new path has to be sent, and the old one has to be noticed as gone."""
    here = folder.resolve()
    handler, _ = collector(folder, config)

    handler.on_moved(FileMovedEvent(str(here / "old.md"), str(here / "new.md")))

    assert handler.drain() is None


def test_a_directory_move_forces_a_full_rescan(
    folder: Path, config: OvSyncConfig
) -> None:
    """One event covers every file under it, and names none of them."""
    here = folder.resolve()
    handler, wakeup = collector(folder, config)

    handler.on_moved(DirMovedEvent(str(here / "a"), str(here / "b")))

    assert wakeup.is_set()
    assert handler.drain() is None


def test_drain_resets_the_collector(folder: Path, config: OvSyncConfig) -> None:
    """A path already synced must not be re-sent on the next wake."""
    handler, _ = collector(folder, config)
    handler.on_modified(FileModifiedEvent(str(folder.resolve() / "note.md")))

    assert handler.drain() == {"note.md"}
    assert handler.drain() == set()


def test_event_mode_syncs_a_backlog_on_startup(
    folder: Path, client: OvClient, config: OvSyncConfig, routes: respx.MockRouter
) -> None:
    """Files edited while the watcher was stopped produced no event.

    Without the startup sync they are never sent: the loop only ever syncs
    the paths an event names, and there was no event.

    Stopping the watcher before running it is what makes this deterministic.
    The loop body never executes, so the single sync that happens can only be
    the startup one — no filesystem event can be credited with it.
    """
    write(folder / "edited-while-down.md", "hello")
    watcher = FolderWatcher(folder, ROOT, client, config)
    watcher.stop()

    watcher.run()

    assert routes["batch_write"].call_count == 1
    with SyncState(folder / config.sync.state_file) as state:
        assert set(state.all_files()) == {"edited-while-down.md"}


def test_a_real_edit_reaches_the_server(
    folder: Path, client: OvClient, config: OvSyncConfig, routes: respx.MockRouter
) -> None:
    """End to end through watchdog, so the wiring itself is covered."""
    watcher = FolderWatcher(folder, ROOT, client, config)
    thread = threading.Thread(target=watcher.run)
    thread.start()
    try:
        # Let the startup sync finish before making the edit, so the sync
        # under test is the one the event triggers.
        time.sleep(0.3)
        write(folder / "note.md", "written while watching")

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with SyncState(folder / config.sync.state_file) as state:
                if "note.md" in state.all_files():
                    break
            time.sleep(0.05)
    finally:
        watcher.stop()
        thread.join(timeout=10)

    with SyncState(folder / config.sync.state_file) as state:
        assert "note.md" in state.all_files()


def test_poll_mode_syncs_immediately(
    folder: Path, client: OvClient, routes: respx.MockRouter
) -> None:
    """Poll mode syncs before its first sleep, not after it."""
    write(folder / "note.md", "hello")
    config = OvSyncConfig(
        sync=SyncConfig(root_uri=ROOT),
        watch=WatchConfig(mode="poll", poll_interval_seconds=30),
    )
    watcher = FolderWatcher(folder, ROOT, client, config)

    thread = threading.Thread(target=watcher.run)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not routes["batch_write"].called:
            time.sleep(0.02)
    finally:
        watcher.stop()
        thread.join(timeout=5)

    assert routes["batch_write"].call_count == 1


def test_the_watcher_reports_every_sync(
    folder: Path, client: OvClient, config: OvSyncConfig, routes: respx.MockRouter
) -> None:
    """The callback is what the CLI prints from, so it has to fire."""
    write(folder / "note.md", "hello")
    seen: list[int] = []
    watcher = FolderWatcher(
        folder, ROOT, client, config, on_sync=lambda r: seen.append(r.created)
    )

    watcher.sync_once()

    assert seen == [1]


def test_the_quiet_period_restarts_on_a_fresh_event(
    folder: Path, client: OvClient, config: OvSyncConfig
) -> None:
    """A burst of saves becomes one sync, not one sync per save."""
    watcher = FolderWatcher(folder, ROOT, client, config)
    wakeup = threading.Event()
    debounce = config.watch.debounce_seconds

    def keep_poking() -> None:
        """Fire three events, each inside the previous one's quiet period."""
        for _ in range(3):
            time.sleep(debounce * 0.5)
            wakeup.set()

    poker = threading.Thread(target=keep_poking)
    started = time.monotonic()
    poker.start()
    watcher._settle(wakeup)
    elapsed = time.monotonic() - started
    poker.join()

    # A fixed delay would have returned after one debounce. Restarting the
    # clock three times cannot finish before two.
    assert elapsed > debounce * 2


def test_the_quiet_period_gives_up_eventually(folder: Path, client: OvClient) -> None:
    """Unbounded waiting is a stall: a file written to constantly is never synced.

    Anything touching a synced file more often than the debounce would park
    the loop here forever, and the periodic rescan cannot run while it is
    parked.

    Run on a thread and joined with a deadline, so an unbounded wait fails the
    test instead of hanging the suite.
    """
    config = OvSyncConfig(
        sync=SyncConfig(root_uri=ROOT), watch=WatchConfig(debounce_seconds=0.05)
    )
    watcher = FolderWatcher(folder, ROOT, client, config)
    wakeup = threading.Event()
    stop_poking = threading.Event()
    cap = config.watch.debounce_seconds * MAX_SETTLE_PERIODS

    def poke_forever() -> None:
        """Never let the folder go quiet."""
        while not stop_poking.is_set():
            wakeup.set()
            time.sleep(0.01)

    elapsed: list[float] = []

    def settle() -> None:
        """Time how long the quiet-period wait takes to give up."""
        started = time.monotonic()
        watcher._settle(wakeup)
        elapsed.append(time.monotonic() - started)

    poker = threading.Thread(target=poke_forever)
    settler = threading.Thread(target=settle, daemon=True)
    poker.start()
    settler.start()
    try:
        settler.join(timeout=cap * 4)
    finally:
        stop_poking.set()
        poker.join()
        watcher.stop()

    assert not settler.is_alive(), f"_settle did not give up within {cap * 4}s"
    assert elapsed[0] >= cap


def test_an_overdue_rescan_runs_even_while_events_arrive() -> None:
    """Gating it on an idle folder skips it exactly when it is needed.

    Watchdog drops events when its queue is busy, so a busy folder is the one
    most likely to have a file the events never mentioned.
    """
    pending = plan_pass(woken=True, drained={"note.md"}, overdue=True)

    assert pending.should_sync is True
    assert pending.scope is None
    assert pending.resets_reconcile is True


def test_a_partial_pass_does_not_discharge_the_rescan() -> None:
    """Otherwise every save pushes the deadline out and the rescan never runs.

    An editing session saves far more often than the 15-minute interval, so
    crediting a partial sync means a dropped event is never caught up.
    """
    pending = plan_pass(woken=True, drained={"note.md"}, overdue=False)

    assert pending.should_sync is True
    assert pending.scope == {"note.md"}
    assert pending.resets_reconcile is False


def test_a_full_pass_discharges_the_rescan() -> None:
    """A drain of None already looked at everything, so it counts."""
    pending = plan_pass(woken=True, drained=None, overdue=False)

    assert pending.should_sync is True
    assert pending.scope is None
    assert pending.resets_reconcile is True


def test_an_idle_turn_syncs_nothing() -> None:
    """No event and no rescan owed means no work, and no wasted scan."""
    pending = plan_pass(woken=False, drained=set(), overdue=False)

    assert pending.should_sync is False
    assert pending.resets_reconcile is False


def test_an_idle_turn_still_runs_an_overdue_rescan() -> None:
    """The quiet path is how the rescan fires on a folder nobody is editing."""
    pending = plan_pass(woken=False, drained=set(), overdue=True)

    assert pending.should_sync is True
    assert pending.scope is None
    assert pending.resets_reconcile is True


def test_a_failed_sync_is_reported_and_the_watch_continues(
    folder: Path, client: OvClient, config: OvSyncConfig
) -> None:
    """A watched folder on a removable volume can vanish for a second.

    ``sync`` turns a server error into ``result.errors``, but the scan raises
    outright when the folder is gone. Letting that propagate would end a
    long-running watch with a traceback instead of retrying next cycle.
    """
    seen: list[Exception] = []
    watcher = FolderWatcher(folder, ROOT, client, config, on_error=seen.append)

    def explode(only: set[str] | None = None) -> SyncResult:
        """Fail the way an unmounted volume does."""
        raise NotADirectoryError("volume went away")

    watcher.sync_once = explode  # type: ignore[method-assign]
    watcher._sync_guarded()

    assert [type(exc) for exc in seen] == [NotADirectoryError]


def test_without_a_handler_a_failed_sync_propagates(
    folder: Path, client: OvClient, config: OvSyncConfig
) -> None:
    """A caller who did not ask for errors to be swallowed does not get that."""
    watcher = FolderWatcher(folder, ROOT, client, config)

    def explode(only: set[str] | None = None) -> SyncResult:
        """Fail the way an unmounted volume does."""
        raise NotADirectoryError("volume went away")

    watcher.sync_once = explode  # type: ignore[method-assign]

    with pytest.raises(NotADirectoryError):
        watcher._sync_guarded()
