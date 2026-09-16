"""Tests for adopting files OpenViking already holds.

The pass exists to end a loop: a run whose batches time out records nothing, so
the next run sends the same files again. What matters here is that a file is
adopted only on a byte-for-byte match, and that adopting it actually takes it
off the next run's list.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from helpers import write

from ov_sync.client import OvClient
from ov_sync.config import Credentials, SyncConfig
from ov_sync.engine import plan_sync
from ov_sync.reconcile import _COMMIT_EVERY, reconcile
from ov_sync.scanner import LocalFile
from ov_sync.state import RootUriMismatch, SyncState

BASE = "https://openviking.example/api/v1"
ROOT = "viking://resources/notes"

# Files written by the chunked-commit test. A fixed number above _COMMIT_EVERY,
# so the pass spans more than one chunk. Fixed rather than computed from the
# constant: a test that sizes itself from the thing it is checking will happily
# try to write a billion files when that thing is wrong.
_CHUNK_TEST_FILES = 60


def ok(result: Any) -> httpx.Response:
    """Build a successful OpenViking response envelope."""
    return httpx.Response(200, json={"status": "ok", "result": result, "error": None})


def listing(*entries: tuple[str, int]) -> httpx.Response:
    """Build a ``/fs/tree`` response from ``(rel_path, size)`` pairs."""
    return ok(
        [
            {"rel_path": rel_path, "size": size, "isDir": False, "modTime": None}
            for rel_path, size in entries
        ]
    )


@pytest.fixture
def client(credentials: Credentials) -> Iterator[OvClient]:
    """An OvClient pointed at the intercepted base URL."""
    with OvClient(credentials) as open_client:
        yield open_client


@pytest.fixture
def state(folder: Path) -> Iterator[SyncState]:
    """An open, empty state database for the folder."""
    with SyncState(folder / ".ov-sync.db") as open_state:
        yield open_state


def candidates(
    folder: Path, sync_config: SyncConfig, state: SyncState
) -> list[LocalFile]:
    """Return the files a sync would send, which is what a reconcile checks."""
    _, detail = plan_sync(folder, sync_config, state)
    return detail.to_write


@respx.mock
def test_a_matching_file_is_adopted_without_being_sent(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """The stored copy matches, so the file is recorded as sent and not sent."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"hello")
    )
    batch = respx.post(f"{BASE}/content/batch-write")

    result = reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    assert result.adopted == 1
    assert result.still_to_send == 0
    assert batch.call_count == 0
    row = state.all_files()["a.md"]
    assert row.digest == hashlib.sha256(b"hello").hexdigest()
    assert row.uri == f"{ROOT}/a.md"


@respx.mock
def test_an_adopted_file_drops_off_the_next_run(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """The whole point: the next plan has nothing left to send for it."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    plan, _ = plan_sync(folder, sync_config, state)
    assert plan.write == []


@respx.mock
def test_a_file_stored_with_other_content_is_not_adopted(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """Same size, different bytes. Adopting it would skip the file forever."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"world")
    )

    result = reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    assert result.adopted == 0
    assert result.differs == 1
    assert state.all_files() == {}


@respx.mock
def test_a_file_the_server_does_not_hold_is_not_downloaded(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """Absent from the listing is answer enough; no round trip is spent on it."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing())
    download = respx.get(f"{BASE}/content/download")

    result = reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    assert result.absent == 1
    assert download.call_count == 0
    assert state.all_files() == {}


@respx.mock
def test_a_size_mismatch_skips_the_download(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """Different sizes cannot hash the same, so the bytes are never fetched."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 99)))
    download = respx.get(f"{BASE}/content/download")

    result = reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    assert result.differs == 1
    assert download.call_count == 0


@respx.mock
def test_a_dry_run_writes_nothing(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """It reports the match and leaves the state alone."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    result = reconcile(
        folder,
        ROOT,
        client,
        sync_config,
        state,
        candidates(folder, sync_config, state),
        dry_run=True,
    )

    assert result.adopted == 1
    assert state.all_files() == {}


@respx.mock
def test_a_failed_download_leaves_the_file_to_be_sent(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """A check that could not be made costs an upload, never a skip."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(
            400,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "INVALID", "message": "bad uri", "details": None},
            },
        )
    )

    result = reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    assert result.adopted == 0
    assert result.errors and "bad uri" in result.errors[0]
    assert state.all_files() == {}


@respx.mock
def test_a_renamed_file_is_looked_up_where_it_landed(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """A rewritten name is looked up where the file actually landed.

    A name a URI cannot hold is rewritten on the way through, so the stored
    copy sits under the new name, not the one on disk.
    """
    write(folder / "a#b.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a_b.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    result = reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    assert result.adopted == 1
    assert state.all_files()["a#b.md"].uri == f"{ROOT}/a_b.md"


@respx.mock
def test_partial_progress_survives_an_interruption(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """Adoptions are committed as they go, so a long pass is not all-or-nothing.

    The download for the second file raises, which is what a Ctrl-C part-way
    amounts to from the state's point of view: the first file's row must
    already be on disk. The scanner sorts by path, so ``a.md`` is always the
    one that got through.

    Raised from a callable rather than listed as a side effect because respx
    only re-raises an ``Exception``, and a KeyboardInterrupt is not one.
    """
    write(folder / "a.md", "hello")
    write(folder / "b.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5), ("b.md", 5)))
    served: list[str] = []

    def interrupt_after_the_first(request: httpx.Request) -> httpx.Response:
        """Answer the first download, then interrupt the pass."""
        served.append(str(request.url))
        if len(served) == 1:
            return httpx.Response(200, content=b"hello")
        raise KeyboardInterrupt

    respx.get(f"{BASE}/content/download").mock(side_effect=interrupt_after_the_first)

    with pytest.raises(KeyboardInterrupt):
        reconcile(
            folder,
            ROOT,
            client,
            sync_config,
            state,
            candidates(folder, sync_config, state),
        )

    assert set(state.all_files()) == {"a.md"}


@respx.mock
def test_reconcile_binds_the_root_it_adopted_against(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """Every adopted row carries a URI under this root, so the root is recorded.

    Without it the state has rows but no root, a later run can be pointed
    somewhere else with nothing to compare against, and the adopted digests
    make every file read as already-synced. Not one byte would reach the new
    root, and nothing would say so.
    """
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    assert state.root_uri == ROOT
    with pytest.raises(RootUriMismatch):
        state.check_root("viking://resources/somewhere-else")


@respx.mock
def test_a_dry_run_does_not_bind_the_root(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """It records nothing, so it commits the folder to nothing either."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    reconcile(
        folder,
        ROOT,
        client,
        sync_config,
        state,
        candidates(folder, sync_config, state),
        dry_run=True,
    )

    assert state.root_uri is None


@respx.mock
def test_a_failed_check_is_counted_as_still_to_send(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """The headline number must not contradict what the next run does."""
    write(folder / "a.md", "hello")
    respx.get(f"{BASE}/fs/tree").mock(return_value=listing(("a.md", 5)))
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(
            400,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "INVALID", "message": "bad uri", "details": None},
            },
        )
    )

    result = reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    plan, _ = plan_sync(folder, sync_config, state)
    assert result.still_to_send == len(plan.write) == 1


@respx.mock
def test_adoptions_are_committed_in_chunks_as_the_pass_runs(
    folder: Path, sync_config: SyncConfig, state: SyncState, client: OvClient
) -> None:
    """Rows reach disk before the pass ends, not only when it finishes.

    A pass over a few thousand files runs for many minutes, and a Ctrl-C part
    way through should keep what it already verified.

    Driven by the real ``_COMMIT_EVERY`` rather than a patched one: patching it
    would override whatever the module actually carries, so the test would pass
    just as happily if the chunking were taken out. The file count is a fixed
    number above it, not derived from it — deriving it means a broken constant
    makes the test try to create that many files rather than fail.

    The row count is read through a second connection, mid-pass, because the
    pass's own connection would show rows it has not committed.
    """
    assert _COMMIT_EVERY < _CHUNK_TEST_FILES, (
        f"this test needs more than one chunk: _COMMIT_EVERY is {_COMMIT_EVERY}, "
        f"and it writes {_CHUNK_TEST_FILES} files"
    )
    names = [f"{index:03d}.md" for index in range(_CHUNK_TEST_FILES)]
    for name in names:
        write(folder / name, "hello")
    respx.get(f"{BASE}/fs/tree").mock(
        return_value=listing(*((name, 5) for name in names))
    )
    seen: list[int] = []

    def count_rows_then_answer(request: httpx.Request) -> httpx.Response:
        """Report how many rows are on disk before answering each download."""
        with sqlite3.connect(folder / ".ov-sync.db") as probe:
            seen.append(probe.execute("SELECT COUNT(*) FROM synced_files").fetchone()[0])
        return httpx.Response(200, content=b"hello")

    respx.get(f"{BASE}/content/download").mock(side_effect=count_rows_then_answer)

    reconcile(
        folder, ROOT, client, sync_config, state, candidates(folder, sync_config, state)
    )

    # Nothing on disk until the first chunk closes, and by the last file a
    # whole chunk is already there — which is what an interrupt would keep.
    assert seen[0] == 0
    assert seen[-1] == _COMMIT_EVERY
    assert len(state.all_files()) == _CHUNK_TEST_FILES
