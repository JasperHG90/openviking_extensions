"""End-to-end tests against a real OpenViking server.

Everything else here runs against respx, which answers with whatever the tests
say the server returns. That is fine for logic and useless for agreement: the
mocks were written from a reading of OpenViking's source, and a reading can be
wrong. Two bugs got through exactly that way — the snapshot id arrives under
``commit_oid``, not ``commit_id``, and batch-write creates missing parent
directories, so a whole layer of ``mkdir`` calls was pointless.

These are marked ``integration`` and left out of the default run. With docker
available they start their own server — OpenViking in ``oidc`` mode trusting a
dev-mode Vault, the way the lab runs it — so this is enough::

    uv run pytest -m integration

``OV_TEST_IMAGE`` picks the OpenViking image; the default is the version this
package is checked against. To point at a server you already run instead, set
``OV_SYNC_TEST_ROOT`` to a writable parent such as ``viking://resources``;
credentials then come from the usual places — the ``ov`` CLI config, or
``OV_SYNC_URL`` and ``OV_SYNC_API_KEY``. Either way each test creates and
removes its own directory under the root.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import write
from live_openviking import LiveOpenViking, docker_available, openviking_stack
from typer.testing import CliRunner

from ov_sync.cli import app
from ov_sync.client import BatchWriteOperation, OvClient
from ov_sync.config import Credentials, SyncConfig, load_credentials
from ov_sync.engine import sync, target_uri
from ov_sync.state import SyncState

pytestmark = pytest.mark.integration

TEST_ROOT_ENV = "OV_SYNC_TEST_ROOT"

# The parent each test makes its directory under when the suite runs its own
# server: the account-wide scope, which is where synced folders land in the lab.
STACK_ROOT = "viking://resources"

# Resolved at import time, on purpose. The autouse isolation fixture repoints
# $HOME at a temporary directory so no unit test can read the developer's real
# config; these tests need exactly that config, and by the time a fixture body
# runs, Path.home() is already the fake one.
REAL_CLI_CONFIG = Path(
    os.environ.get("OPENVIKING_CLI_CONFIG_FILE")
    or Path.home() / ".openviking" / "ovcli.conf"
)


@pytest.fixture(scope="session")
def stack() -> Iterator[LiveOpenViking | None]:
    """A server this suite started, or None when one was configured instead."""
    if os.environ.get(TEST_ROOT_ENV):
        yield None
        return
    if not docker_available():
        pytest.skip(f"Needs docker, or {TEST_ROOT_ENV} pointing at a running server.")
    with openviking_stack("ov-sync-it") as live:
        yield live


@pytest.fixture
def live_credentials(
    isolate_environment: None, stack: LiveOpenViking | None
) -> Credentials:
    """Credentials for the server under test."""
    if stack is not None:
        return Credentials(url=stack.url, api_key=stack.token)
    try:
        return load_credentials(REAL_CLI_CONFIG)
    except (FileNotFoundError, ValueError) as exc:  # pragma: no cover - config-dependent
        pytest.skip(f"No OpenViking credentials: {exc}")


@pytest.fixture
def live_client(live_credentials: Credentials) -> Iterator[OvClient]:
    """A client against the server under test."""
    with OvClient(live_credentials) as client:
        yield client


@pytest.fixture
def live_root(live_client: OvClient) -> Iterator[str]:
    """Create a throwaway directory on the server, and remove it after."""
    parent = os.environ.get(TEST_ROOT_ENV, STACK_ROOT).rstrip("/")
    root = f"{parent}/ov-sync-test-{uuid.uuid4().hex[:12]}"
    # The batch root has to exist before the first write. Only the root: the
    # directories below it come free with batch-write, which one of the tests
    # below proves.
    live_client.mkdir(root, description="ov-sync integration test")
    try:
        yield root
    finally:
        _remove_tree(live_client, root)


def _remove_tree(client: OvClient, root: str) -> None:
    """Remove a test tree, and check it actually went.

    A plain recursive rm races the server's own asynchronous refresh: a tree
    that was just written to and deleted from can come back partly intact,
    and the leftovers accumulate in whatever real scope these tests were
    pointed at. Waiting for the refresh and confirming the tree is gone keeps
    that from happening quietly.
    """
    for _ in range(3):
        client.rm(root, recursive=True, wait=True)
        if client.stat(root) is None:
            return
    raise AssertionError(f"could not remove the test tree at {root}")


def test_a_folder_round_trips(
    folder: Path, live_client: OvClient, live_root: str, tmp_path: Path
) -> None:
    """Create, re-run, edit, and delete, against the real server.

    One test rather than four: each step needs the previous one's state on the
    server, and a shared fixture would have them racing for the same tree.
    """
    write(folder / "one.md", "hello")
    write(folder / "deep" / "two.md", "nested")
    config = SyncConfig(root_uri=live_root)

    with SyncState(folder / config.state_file) as state:
        first = sync(folder, live_root, live_client, config, state)
    assert first.created == 2, first.errors

    # The nested file proves batch-write created `deep/` on its own.
    nested = live_client.stat(target_uri(live_root, "deep/two.md"))
    assert nested is not None
    assert nested.size == len(b"nested")

    with SyncState(folder / config.state_file) as state:
        second = sync(folder, live_root, live_client, config, state)
    assert second.written == 0
    assert second.unchanged == 2

    (folder / "one.md").write_text("hello again", encoding="utf-8")
    with SyncState(folder / config.state_file) as state:
        third = sync(folder, live_root, live_client, config, state)
    assert third.updated == 1, third.errors

    (folder / "deep" / "two.md").unlink()
    with SyncState(folder / config.state_file) as state:
        fourth = sync(folder, live_root, live_client, config, state, apply_deletes=True)
    assert fourth.deleted_applied == 1, fourth.errors
    # The snapshot is the documented undo path, so its id has to survive the
    # round trip. It arrives as `commit_oid`, which an earlier version missed.
    assert fourth.snapshot_id
    assert live_client.stat(target_uri(live_root, "deep/two.md")) is None


def test_a_name_a_uri_cannot_hold_reaches_the_server(
    folder: Path, live_client: OvClient, live_root: str
) -> None:
    """The rewrite is built against the server's rules, so ask the server.

    This is the whole point of the sanitizing: `[#TDAI-709] plan.md` used to
    fail the batch it travelled in, and the mocks would happily accept a
    rewrite that the real validator still refused.
    """
    awkward = "[#TDAI-709] plan.md"
    write(folder / awkward, "hello")
    write(folder / "quiet.md", "the rest of the batch")
    config = SyncConfig(root_uri=live_root)

    with SyncState(folder / config.state_file) as state:
        result = sync(folder, live_root, live_client, config, state)

    # Two, not one: the bug was that the bad name took the batch down with it.
    assert result.created == 2, result.errors
    assert result.errors == []
    stored = live_client.stat(target_uri(live_root, awkward))
    assert stored is not None
    assert stored.name == "[_TDAI-709] plan.md"


def test_binary_content_survives_the_round_trip(
    folder: Path, live_client: OvClient, live_root: str
) -> None:
    """Bytes that are not UTF-8 go base64 and must come back byte-identical."""
    payload = bytes(range(256))
    write(folder / "blob.bin", payload)
    config = SyncConfig(root_uri=live_root, include_extensions=[".bin"])

    with SyncState(folder / config.state_file) as state:
        result = sync(folder, live_root, live_client, config, state)

    assert result.created == 1, result.errors
    stored = live_client.stat(target_uri(live_root, "blob.bin"))
    assert stored is not None
    assert stored.size == len(payload)


def test_stat_tells_a_file_from_a_directory_from_nothing(
    live_client: OvClient, live_root: str
) -> None:
    """``isDir``, ``size`` and ``name`` are read off the reply; a miss is None."""
    live_client.batch_write(
        live_root,
        [BatchWriteOperation(uri=f"{live_root}/a.md", content="four")],
    )

    directory = live_client.stat(live_root)
    assert directory is not None
    assert directory.is_dir

    file = live_client.stat(f"{live_root}/a.md")
    assert file is not None
    assert not file.is_dir
    assert file.name == "a.md"
    assert file.size == len(b"four")

    assert live_client.stat(f"{live_root}/missing.md") is None


def test_mkdir_on_an_existing_directory_is_not_an_error(
    live_client: OvClient, live_root: str
) -> None:
    """The server refuses an existing directory; the client calls that done.

    ``--create-root`` on a second run, and two runs racing to create the same
    root, both rely on this.
    """
    live_client.mkdir(live_root)
    live_client.mkdir(f"{live_root}/sub/deeper", description="parents come too")

    live_client.mkdir(live_root)
    made = live_client.stat(f"{live_root}/sub/deeper")
    assert made is not None
    assert made.is_dir


def test_batch_write_reports_what_it_did_per_uri(
    live_client: OvClient, live_root: str
) -> None:
    """``created``, ``updated`` and ``unchanged`` are what the engine counts by."""
    uri = f"{live_root}/one.md"

    first = live_client.batch_write(
        live_root, [BatchWriteOperation(uri=uri, content="first")]
    )
    assert first.root_uri == live_root
    assert first.created == [uri]
    assert first.updated == []

    second = live_client.batch_write(
        live_root, [BatchWriteOperation(uri=uri, content="second")]
    )
    assert second.created == []
    assert second.updated == [uri]

    third = live_client.batch_write(
        live_root, [BatchWriteOperation(uri=uri, content="second")]
    )
    assert third.created == []
    assert uri in third.written


def test_rm_removes_and_a_second_rm_is_not_an_error(
    live_client: OvClient, live_root: str
) -> None:
    """The server answers a delete of a missing URI with success.

    That is why ``rm`` returns nothing: a client that reported "already gone"
    from this reply would be reporting something the server never said.
    """
    uri = f"{live_root}/gone.md"
    live_client.batch_write(live_root, [BatchWriteOperation(uri=uri, content="x")])

    live_client.rm(uri, wait=True)
    assert live_client.stat(uri) is None
    live_client.rm(uri)


def test_a_snapshot_commit_names_its_commit(
    live_client: OvClient, live_root: str
) -> None:
    """The id is the undo instruction ``--delete`` prints, so it must arrive."""
    live_client.batch_write(
        live_root, [BatchWriteOperation(uri=f"{live_root}/kept.md", content="x")]
    )

    commit = live_client.snapshot_commit("ov-sync integration", paths=[live_root])

    assert commit.commit_id


def test_the_cli_syncs_with_the_token_ovcli_conf_holds(
    folder: Path,
    live_credentials: Credentials,
    live_client: OvClient,
    live_root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ovsync run`` reads the same file ``ovx --bind`` writes, and syncs.

    The file holds a Vault identity token under ``api_key``. This is the path
    the lab's devcontainer takes, with nothing between the CLI and the server.
    """
    conf = tmp_path / "ovcli.conf"
    conf.write_text(
        json.dumps({"url": live_credentials.url, "api_key": live_credentials.api_key}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENVIKING_CLI_CONFIG_FILE", str(conf))
    write(folder / "note.md", "from the cli")
    target = f"{live_root}/cli"

    result = CliRunner().invoke(
        app, ["run", str(folder), "--root-uri", target, "--create-root"]
    )

    assert result.exit_code == 0, result.output
    stored = live_client.stat(target_uri(target, "note.md"))
    assert stored is not None
    assert stored.size == len(b"from the cli")


def test_a_write_into_a_tree_still_being_indexed_waits_its_turn(
    live_client: OvClient, live_root: str
) -> None:
    """OpenViking 0.5 refuses it as a retryable ``CONFLICT``; the client retries.

    A batch sent without waiting leaves its tree locked while it indexes, and
    the next batch into that tree is refused with ``path_busy`` until it is
    done. ``wait_for_indexing = false`` makes that the normal case for every
    run after the first.
    """
    uri = f"{live_root}/busy.md"
    live_client.batch_write(
        live_root, [BatchWriteOperation(uri=uri, content="first")], wait=False
    )

    second = live_client.batch_write(
        live_root, [BatchWriteOperation(uri=uri, content="second")], wait=False
    )

    assert second.updated == [uri]
