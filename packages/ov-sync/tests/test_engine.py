"""Tests for the sync itself: what gets sent, what does not, and what is recorded."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from helpers import write

from ov_sync.client import OvClient
from ov_sync.config import SERVER_MAX_FILE_BYTES, Credentials, SyncConfig
from ov_sync.engine import sync, target_uri
from ov_sync.scanner import LocalFile
from ov_sync.state import RootUriMismatch, SyncState

BASE = "https://openviking.example/api/v1"
ROOT = "viking://resources/notes"


def ok(result: Any) -> httpx.Response:
    """Build a successful OpenViking response envelope."""
    return httpx.Response(200, json={"status": "ok", "result": result, "error": None})


def batch_reply(request: httpx.Request) -> httpx.Response:
    """Answer a batch-write by echoing every target back as created."""
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
def state(folder: Path) -> Iterator[SyncState]:
    """An open state database in the synced folder."""
    with SyncState(folder / ".ov-sync.db") as open_state:
        yield open_state


@pytest.fixture
def routes() -> Iterator[respx.MockRouter]:
    """Intercept every endpoint a sync touches, with sensible defaults.

    ``assert_all_called`` is off because the point of most of these tests is
    that some endpoint was *not* reached.
    """
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/content/batch-write", name="batch_write").mock(
            side_effect=batch_reply
        )
        mock.delete("/fs", name="rm").mock(return_value=ok({"uri": "x"}))
        mock.post("/snapshot/commit", name="snapshot").mock(
            return_value=ok({"commit_id": "snap-1"})
        )
        yield mock


def batches(routes: respx.MockRouter) -> list[dict[str, Any]]:
    """Return the parsed body of every batch-write request made."""
    return [json.loads(call.request.content) for call in routes["batch_write"].calls]


def operations(routes: respx.MockRouter) -> list[dict[str, Any]]:
    """Return every operation sent, across all batches."""
    return [operation for body in batches(routes) for operation in body["operations"]]


def test_target_uri_maps_a_path_onto_the_root() -> None:
    """The URI is the key, so the mapping has to be positional and stable."""
    assert target_uri(ROOT, "sub/note.md") == f"{ROOT}/sub/note.md"
    assert target_uri(f"{ROOT}/", "note.md") == f"{ROOT}/note.md"


def test_a_name_a_uri_cannot_hold_is_rewritten_and_sent(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """A `#` in a file name used to fail the whole batch it travelled in."""
    write(folder / "tickets" / "[#TDAI-709] hub and spoke.md", "hello")

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.created == 1
    assert operations(routes) == [
        {
            "uri": f"{ROOT}/tickets/[_TDAI-709] hub and spoke.md",
            "content": "hello",
            "mode": "upsert",
        }
    ]
    assert result.plan.renamed[0].relative_path == "tickets/[#TDAI-709] hub and spoke.md"
    assert result.plan.renamed[0].uri_path == "tickets/[_TDAI-709] hub and spoke.md"


def test_a_rewritten_file_is_recorded_under_the_uri_it_landed_on(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The state has to point where the file actually went, or a delete misses."""
    write(folder / "a#b.md", "hello")

    sync(folder, ROOT, client, sync_config, state)

    assert state.all_files()["a#b.md"].uri == f"{ROOT}/a_b.md"


def test_a_rewritten_file_is_not_re_sent_next_run(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The rewrite is part of the key, so it settles like any other file."""
    write(folder / "a#b.md", "hello")
    sync(folder, ROOT, client, sync_config, state)

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.written == 0
    assert result.unchanged == 1
    assert result.plan.renamed == []
    assert len(batches(routes)) == 1


def test_two_files_that_want_one_uri_are_both_held_back(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Sending both would have the second silently overwrite the first."""
    write(folder / "a#b.md", "first")
    write(folder / "a?b.md", "second")
    write(folder / "fine.md", "untouched by any of this")

    result = sync(folder, ROOT, client, sync_config, state)

    assert operations(routes) == [
        {
            "uri": f"{ROOT}/fine.md",
            "content": "untouched by any of this",
            "mode": "upsert",
        }
    ]
    assert sorted(skipped.relative_path for skipped in result.skipped) == [
        "a#b.md",
        "a?b.md",
    ]
    assert set(state.all_files()) == {"fine.md"}


def test_the_file_that_owns_the_name_is_the_one_that_goes(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """A rewrite never evicts the file whose name was safe all along."""
    write(folder / "a_b.md", "the real one")
    write(folder / "a#b.md", "the impostor")

    result = sync(folder, ROOT, client, sync_config, state)

    assert operations(routes) == [
        {"uri": f"{ROOT}/a_b.md", "content": "the real one", "mode": "upsert"}
    ]
    assert [skipped.relative_path for skipped in result.skipped] == ["a#b.md"]


def test_renaming_a_file_onto_its_own_uri_does_not_delete_it(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The fix for a clash must not destroy the file it fixes.

    `a#b.md` lands at `a_b.md`. Renaming it to `a_b.md` on disk — what the
    clash message asks for — leaves a state row pointing at the URI the
    renamed file now occupies. Acting on that row as a deletion would remove
    what this run just wrote, and the state would still call it synced.
    """
    path = write(folder / "a#b.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.rename(folder / "a_b.md")

    result = sync(folder, ROOT, client, sync_config, state, apply_deletes=True)

    assert routes["rm"].calls == []
    assert result.deleted_detected == []
    assert result.deleted_applied == 0
    # The old row is gone, so nothing claims a file is synced that is not.
    assert set(state.all_files()) == {"a_b.md"}


def test_the_uri_a_file_still_occupies_is_never_deleted_later(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """A later run must not act on the stale row either, --delete or not."""
    path = write(folder / "a#b.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.rename(folder / "a_b.md")
    sync(folder, ROOT, client, sync_config, state)

    result = sync(folder, ROOT, client, sync_config, state, apply_deletes=True)

    assert routes["rm"].calls == []
    assert result.unchanged == 1


def test_a_real_deletion_is_still_reported(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Guarding the rename case must not blunt ordinary deletion."""
    path = write(folder / "a#b.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()

    result = sync(folder, ROOT, client, sync_config, state, apply_deletes=True)

    assert result.deleted_detected == [f"{ROOT}/a_b.md"]
    assert result.deleted_applied == 1
    assert state.all_files() == {}


def test_a_blocked_file_stops_claiming_a_uri_it_lost(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The outright owner takes the URI, so the rewritten row is no longer true."""
    write(folder / "a#b.md", "the rewritten one")
    sync(folder, ROOT, client, sync_config, state)
    write(folder / "a_b.md", "the real one")

    result = sync(folder, ROOT, client, sync_config, state)

    assert [skipped.relative_path for skipped in result.skipped] == ["a#b.md"]
    assert set(state.all_files()) == {"a_b.md"}
    assert result.unchanged == 0


def test_replacing_a_synced_copy_is_reported(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Taking a URI over costs a file its copy, so it is not done quietly."""
    write(folder / "a#b.md", "the rewritten one")
    sync(folder, ROOT, client, sync_config, state)
    write(folder / "a_b.md", "the real one")

    result = sync(folder, ROOT, client, sync_config, state)

    assert len(result.plan.taken_over) == 1
    taken = result.plan.taken_over[0]
    assert taken.relative_path == "a#b.md"
    assert taken.taken_by == "a_b.md"
    assert taken.uri == f"{ROOT}/a_b.md"


def test_a_takeover_is_reported_once(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """It is news on the run that does it, not on every run after."""
    write(folder / "a#b.md", "the rewritten one")
    sync(folder, ROOT, client, sync_config, state)
    write(folder / "a_b.md", "the real one")
    sync(folder, ROOT, client, sync_config, state)

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.plan.taken_over == []


def test_a_takeover_is_not_announced_by_a_run_that_will_not_do_it(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Watch mode narrows a run to one file; the rest are somebody else's turn."""
    write(folder / "a#b.md", "the rewritten one")
    sync(folder, ROOT, client, sync_config, state)
    write(folder / "a_b.md", "the real one")
    write(folder / "other.md", "unrelated")

    result = sync(folder, ROOT, client, sync_config, state, only={"other.md"})

    assert result.plan.write == ["other.md"]
    assert result.plan.taken_over == []


def test_a_folder_cannot_be_synced_under_a_file_an_earlier_run_left(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Writing under a stale file node hides the new file from listing.

    OpenViking takes the write and reports success, but `ls` on that node
    returns nothing from then on, and no further write digs it out — only
    removing the file and writing again does. The deletion is merely reported
    until --delete acts on it, so this would otherwise happen on a default run
    that looks completely clean.
    """
    path = write(folder / "notes.md", "the old file")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()
    write(folder / "notes.md" / "inner.md", "the new file")

    result = sync(folder, ROOT, client, sync_config, state)

    # One batch, from the first sync. The second sent nothing.
    assert len(batches(routes)) == 1
    assert result.plan.write == []
    assert [skipped.relative_path for skipped in result.skipped] == ["notes.md/inner.md"]
    assert "--delete" in result.skipped[0].reason
    assert result.deleted_detected == [f"{ROOT}/notes.md"]


def test_a_copy_in_the_way_blocks_even_when_its_file_is_still_here(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The node is in the way because it was synced, not because it is gone.

    `a#b.md` syncs to `a_b.md` and stays on disk. A folder called `a_b.md`
    turns up later. The clash rule holds the rewritten file back, but its copy
    from the first run is still the file at that URI, so anything written
    inside the folder would be buried under it.
    """
    write(folder / "a#b.md", "the rewritten one")
    sync(folder, ROOT, client, sync_config, state)
    write(folder / "a_b.md" / "inner.md", "would be buried")

    result = sync(folder, ROOT, client, sync_config, state)

    assert len(batches(routes)) == 1
    assert result.plan.write == []
    reasons = {skipped.relative_path: skipped.reason for skipped in result.skipped}
    assert "a_b.md/inner.md" in reasons
    assert "synced from 'a#b.md'" in reasons["a_b.md/inner.md"]
    # Still on disk, so a delete alone will not do: it has to be renamed too,
    # or the next run just sends it back to the same URI.
    assert "Rename one of them, then re-run with --delete" in reasons["a_b.md/inner.md"]


def test_nested_nodes_in_the_way_name_the_outermost_one(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Two files in the way, one inside the other: the outer one has to go first.

    Naming `a/b` would send the operator to clear it and find the file still
    blocked, because `a` is a file node above it and still in the way.

    The state is seeded rather than synced into place: this check is what stops
    a run from writing under a node, so the only way to hold rows at `a` and
    `a/b` at once is to have them from a version that had no such check — which
    is exactly whose mess this has to report well.
    """
    rows = [
        (LocalFile(path=folder / name, relative_path=name, mtime=1.0, size=1), uri, "d")
        for name, uri in (("a", f"{ROOT}/a"), ("a/b", f"{ROOT}/a/b"))
    ]
    state.mark_synced(rows)
    write(folder / "a" / "b" / "c.md", "the file that gets buried")

    result = sync(folder, ROOT, client, sync_config, state)

    reason = {skipped.relative_path: skipped.reason for skipped in result.skipped}
    assert "a/b/c.md" in reason
    assert "'a' in OpenViking" in reason["a/b/c.md"]
    assert "'a/b' in OpenViking" not in reason["a/b/c.md"]


def test_the_remedy_for_a_file_still_on_disk_actually_clears_it(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Follow the advice the message gives, and check the block goes away.

    Renaming alone is not enough: the copy the first run sent is still the
    file at that URI until a delete clears it, which is why the message says
    both steps.
    """
    path = write(folder / "a#b.md", "the rewritten one")
    sync(folder, ROOT, client, sync_config, state)
    write(folder / "a_b.md" / "inner.md", "would be buried")

    # Step one: rename it, as the message says.
    path.rename(folder / "ab.md")
    after_rename = sync(folder, ROOT, client, sync_config, state)
    assert [s.relative_path for s in after_rename.skipped] == ["a_b.md/inner.md"]

    # Step two: the delete it also says to run.
    sync(folder, ROOT, client, sync_config, state, apply_deletes=True)
    cleared = sync(folder, ROOT, client, sync_config, state)

    assert cleared.skipped == []
    assert set(state.all_files()) == {"ab.md", "a_b.md/inner.md"}


def test_the_folder_syncs_once_the_stale_file_is_gone(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """--delete clears the node, and the next run sends the folder."""
    path = write(folder / "notes.md", "the old file")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()
    write(folder / "notes.md" / "inner.md", "the new file")
    sync(folder, ROOT, client, sync_config, state, apply_deletes=True)

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.created == 1, result.errors
    assert result.skipped == []
    assert set(state.all_files()) == {"notes.md/inner.md"}


def test_a_failed_write_keeps_the_row_that_still_names_the_uri(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Forgetting the row before the write lands would strand the old copy.

    The row is the only thing naming that URI. Drop it, let the write fail,
    and the copy already in OpenViking is untracked for good: no later run
    would delete it, because nothing remembers it is there.
    """
    path = write(folder / "a#b.md", "the rewritten one")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()
    write(folder / "a_b.md", "the real one")
    routes["batch_write"].mock(side_effect=httpx.ConnectError("kaboom"))

    failed = sync(folder, ROOT, client, sync_config, state)

    assert failed.errors
    assert set(state.all_files()) == {"a#b.md"}

    # And once the write does land, the row goes and the URI has one owner.
    routes["batch_write"].mock(side_effect=batch_reply)
    recovered = sync(folder, ROOT, client, sync_config, state)

    assert recovered.created == 1, recovered.errors
    assert set(state.all_files()) == {"a_b.md"}


def test_a_dry_run_reports_the_clash_without_reading_anything(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The operator finds out before the first byte moves."""
    write(folder / "a#b.md", "first")
    write(folder / "a?b.md", "second")

    result = sync(folder, ROOT, client, sync_config, state, dry_run=True)

    assert result.plan.write == []
    assert len(result.plan.skipped) == 2
    assert batches(routes) == []


def test_a_new_file_is_sent_and_recorded(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The first run sends everything and writes down what it sent."""
    write(folder / "note.md", "hello")

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.created == 1
    assert operations(routes) == [
        {"uri": f"{ROOT}/note.md", "content": "hello", "mode": "upsert"}
    ]
    assert set(state.all_files()) == {"note.md"}


def test_a_second_run_sends_nothing(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Unchanged files are never even opened, let alone re-sent."""
    write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.written == 0
    assert result.unchanged == 1
    assert len(batches(routes)) == 1


def test_a_touched_file_is_hashed_but_not_sent(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """A moved mtime is not an edit, and a re-send would cost a re-embed."""
    path = write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    os.utime(path, (0, 0))

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.touched == 1
    assert result.written == 0
    assert len(batches(routes)) == 1
    # The new mtime is recorded, so the next run does not hash it again.
    assert state.all_files()["note.md"].mtime == path.stat().st_mtime


def test_an_edited_file_is_sent_again(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Different bytes mean a real change."""
    path = write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.write_text("goodbye", encoding="utf-8")

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.written == 1
    assert operations(routes)[-1]["content"] == "goodbye"


def test_full_resends_everything(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """--full exists for when the remote tree is not what the state thinks."""
    write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)

    result = sync(folder, ROOT, client, sync_config, state, full=True)

    assert result.written == 1
    assert len(batches(routes)) == 2
    # A full run re-sends the files the diff called unchanged, so counting
    # them in both columns would report one file twice.
    assert result.unchanged == 0
    assert result.unchanged + result.deferred + len(result.plan.write) == result.scanned


def test_binary_content_goes_as_base64(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Bytes that are not UTF-8 cannot ride in the text field."""
    config = SyncConfig(root_uri=ROOT, include_extensions=[".bin"])
    payload = b"\x00\x01\xff\xfe"
    write(folder / "blob.bin", payload)

    sync(folder, ROOT, client, config, state)

    operation = operations(routes)[0]
    assert "content" not in operation
    assert base64.b64decode(operation["content_base64"]) == payload


def test_a_nested_file_needs_no_mkdir(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """batch-write creates missing parents itself, verified against a real server.

    Pre-creating them cost one round trip per level per new directory. If a
    future OpenViking stops doing it, this test stays green and the sync
    breaks — so the batch-write mock deliberately answers only for operations
    whose URI sits under the root, which is all the server promises.
    """
    write(folder / "a" / "b" / "note.md", "hello")

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.created == 1
    assert [operation["uri"] for operation in operations(routes)] == [
        f"{ROOT}/a/b/note.md"
    ]


def test_batches_respect_the_operation_limit(
    folder: Path, client: OvClient, state: SyncState, routes: respx.MockRouter
) -> None:
    """A folder bigger than one request is split into several."""
    config = SyncConfig(root_uri=ROOT, max_operations_per_batch=2)
    for index in range(5):
        write(folder / f"note{index}.md", f"body {index}")

    result = sync(folder, ROOT, client, config, state)

    assert [len(body["operations"]) for body in batches(routes)] == [2, 2, 1]
    assert result.created == 5


def test_an_oversize_file_is_skipped_with_a_reason(
    folder: Path, client: OvClient, state: SyncState, routes: respx.MockRouter
) -> None:
    """The server refuses a file over 8 MB, so it never leaves the machine."""
    config = SyncConfig(root_uri=ROOT, include_extensions=[".bin"])
    write(folder / "huge.bin", b"x" * (SERVER_MAX_FILE_BYTES + 1))

    result = sync(folder, ROOT, client, config, state)

    assert result.written == 0
    assert [skipped.relative_path for skipped in result.skipped] == ["huge.bin"]
    assert "over the server's" in result.skipped[0].reason
    assert not batches(routes)


def test_a_failed_batch_is_not_recorded(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The server does not say which writes landed, so none are trusted."""
    write(folder / "note.md", "hello")
    routes["batch_write"].mock(
        return_value=httpx.Response(
            400,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "INVALID_ARGUMENT", "message": "nope", "details": None},
            },
        )
    )

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.written == 0
    assert result.errors
    assert state.all_files() == {}


def test_deletions_are_reported_but_not_applied_by_default(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Removing content is opt-in: a run says what it saw first."""
    path = write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()

    result = sync(folder, ROOT, client, sync_config, state)

    assert result.deleted_detected == [f"{ROOT}/note.md"]
    assert result.deleted_applied == 0
    assert routes["rm"].call_count == 0
    # Still tracked, so the next run reports it again rather than forgetting.
    assert set(state.all_files()) == {"note.md"}


def test_deletions_are_applied_behind_a_snapshot(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """The snapshot is the undo path, so it is committed before the first rm."""
    path = write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()

    result = sync(folder, ROOT, client, sync_config, state, apply_deletes=True)

    assert result.deleted_applied == 1
    assert result.snapshot_id == "snap-1"
    assert routes["snapshot"].call_count == 1
    assert state.all_files() == {}


def test_a_failed_snapshot_cancels_the_deletions(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Deleting without the undo path is worse than not deleting."""
    path = write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()
    routes["snapshot"].mock(
        return_value=httpx.Response(
            400,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "INTERNAL", "message": "no git ref", "details": None},
            },
        )
    )

    result = sync(folder, ROOT, client, sync_config, state, apply_deletes=True)

    assert result.deleted_applied == 0
    assert routes["rm"].call_count == 0
    assert any("nothing was deleted" in error for error in result.errors)
    assert set(state.all_files()) == {"note.md"}


def test_deletions_can_skip_the_snapshot(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """--no-snapshot is the escape hatch when snapshots are unavailable."""
    path = write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()

    result = sync(
        folder,
        ROOT,
        client,
        sync_config,
        state,
        apply_deletes=True,
        snapshot_before_delete=False,
    )

    assert result.deleted_applied == 1
    assert routes["snapshot"].call_count == 0


def test_dry_run_sends_nothing(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """A dry run reports the plan and leaves both sides untouched."""
    write(folder / "a" / "note.md", "hello")

    result = sync(folder, ROOT, client, sync_config, state, dry_run=True)

    assert result.plan.write == ["a/note.md"]
    assert not batches(routes)
    assert state.all_files() == {}


def test_only_limits_what_is_written(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Watch mode syncs the files an event named, not the whole folder."""
    write(folder / "one.md", "1")
    write(folder / "two.md", "2")

    sync(folder, ROOT, client, sync_config, state, only={"one.md"})

    assert [operation["uri"] for operation in operations(routes)] == [f"{ROOT}/one.md"]


def test_switching_roots_is_refused(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Every stored URI came from the old root; a silent switch orphans them."""
    write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)

    with pytest.raises(RootUriMismatch):
        sync(folder, "viking://resources/elsewhere", client, sync_config, state)


def test_a_file_removed_and_restored_is_sent_again(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """After a delete lands, the same path coming back is new again."""
    path = write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)
    path.unlink()
    sync(folder, ROOT, client, sync_config, state, apply_deletes=True)

    write(folder / "note.md", "hello")
    result = sync(folder, ROOT, client, sync_config, state)

    assert result.created == 1


def test_a_file_only_holds_back_is_deferred_not_unchanged(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Counting by subtraction called it unchanged, which was the opposite.

    Watch mode narrows a sync to the paths an event named. A file that
    changed but was not named is deferred to a later pass; reporting it as
    unchanged tells the user their edit is safely stored when it is not.
    """
    path_a = write(folder / "a.md", "one")
    path_b = write(folder / "b.md", "two")
    sync(folder, ROOT, client, sync_config, state)
    path_a.write_text("one edited", encoding="utf-8")
    path_b.write_text("two edited", encoding="utf-8")

    result = sync(folder, ROOT, client, sync_config, state, only={"a.md"})

    assert result.written == 1
    assert result.deferred == 1
    assert result.unchanged == 0


def test_a_file_that_grows_mid_run_is_skipped_not_sent(
    folder: Path,
    client: OvClient,
    state: SyncState,
    routes: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan's size is stale by the time the bytes are read.

    A batch sized on the stale number is refused by the server as a whole, so
    one growing file would take every other file in its batch down with it.
    """
    config = SyncConfig(root_uri=ROOT, include_extensions=[".bin"])
    target = folder / "growing.bin"
    write(target, b"small")
    write(folder / "innocent.bin", b"fine")

    original = Path.read_bytes

    def grow_on_read(self: Path) -> bytes:
        """Return an over-limit payload for the one file, as if it had grown."""
        if self.name == "growing.bin":
            return b"x" * (SERVER_MAX_FILE_BYTES + 1)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", grow_on_read)

    result = sync(folder, ROOT, client, config, state)

    assert [skipped.relative_path for skipped in result.skipped] == ["growing.bin"]
    assert [operation["uri"] for operation in operations(routes)] == [
        f"{ROOT}/innocent.bin"
    ]
    assert set(state.all_files()) == {"innocent.bin"}


def test_batches_respect_the_total_byte_limit(
    folder: Path, client: OvClient, state: SyncState, routes: respx.MockRouter
) -> None:
    """Four 5 MB files cannot ride in one 16 MB request."""
    config = SyncConfig(root_uri=ROOT, include_extensions=[".txt"])
    for index in range(4):
        write(folder / f"big{index}.txt", "x" * (5 * 1024 * 1024))

    result = sync(folder, ROOT, client, config, state)

    assert [len(body["operations"]) for body in batches(routes)] == [3, 1]
    assert result.created == 4


def test_batches_are_filled_by_the_bytes_actually_read(
    folder: Path,
    client: OvClient,
    state: SyncState,
    routes: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sizing a batch on the scan's numbers packs a request the server refuses.

    The scan sees three 5-byte files and would put all three in one request.
    By the time they are read they are 6 MB each, which is 18 MB — over the
    16 MB the server takes. It refuses the whole batch, and since that refusal
    is deterministic it is not retried, so all three files fail because the
    accounting used a stale number.
    """
    config = SyncConfig(root_uri=ROOT, include_extensions=[".bin"])
    for index in range(3):
        write(folder / f"grew{index}.bin", b"small")

    grown = b"\x00" * (6 * 1024 * 1024)
    monkeypatch.setattr(Path, "read_bytes", lambda self: grown)

    result = sync(folder, ROOT, client, config, state)

    assert [len(body["operations"]) for body in batches(routes)] == [2, 1]
    assert result.created == 3


def test_a_dry_run_does_not_bind_the_folder_to_a_root(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """A command that promises to change nothing must not change the state.

    Recording the root on a dry run commits the folder to whatever URI was
    typed, and a typo can then only be undone by deleting the state file.
    """
    write(folder / "note.md", "hello")

    sync(folder, "viking://resources/typo", client, sync_config, state, dry_run=True)

    assert state.root_uri is None
    # And the real run that follows is free to pick the right one.
    sync(folder, ROOT, client, sync_config, state)
    assert state.root_uri == ROOT


def test_a_dry_run_still_refuses_a_changed_root(
    folder: Path,
    client: OvClient,
    sync_config: SyncConfig,
    state: SyncState,
    routes: respx.MockRouter,
) -> None:
    """Not writing the root is not the same as not checking it."""
    write(folder / "note.md", "hello")
    sync(folder, ROOT, client, sync_config, state)

    with pytest.raises(RootUriMismatch):
        sync(
            folder,
            "viking://resources/elsewhere",
            client,
            sync_config,
            state,
            dry_run=True,
        )
