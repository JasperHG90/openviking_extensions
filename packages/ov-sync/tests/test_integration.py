"""End-to-end tests against a real OpenViking server.

Everything else here runs against respx, which answers with whatever the tests
say the server returns. That is fine for logic and useless for agreement: the
mocks were written from a reading of OpenViking's source, and a reading can be
wrong. Two bugs got through exactly that way — the snapshot id arrives under
``commit_oid``, not ``commit_id``, and batch-write creates missing parent
directories, so a whole layer of ``mkdir`` calls was pointless.

These tests need a reachable server and a scope they may write to, so they are
marked ``integration`` and left out of the default run::

    OV_SYNC_TEST_ROOT=viking://resources uv run pytest -m integration

The root is a *parent*: each test creates and removes its own directory under
it. Credentials come from the usual places — the ``ov`` CLI config, or
``OV_SYNC_URL`` and ``OV_SYNC_API_KEY``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import write

from ov_sync.client import OvClient
from ov_sync.config import SyncConfig, load_credentials
from ov_sync.engine import sync, target_uri
from ov_sync.state import SyncState

pytestmark = pytest.mark.integration

TEST_ROOT_ENV = "OV_SYNC_TEST_ROOT"

# Resolved at import time, on purpose. The autouse isolation fixture repoints
# $HOME at a temporary directory so no unit test can read the developer's real
# config; these tests need exactly that config, and by the time a fixture body
# runs, Path.home() is already the fake one.
REAL_CLI_CONFIG = Path(
    os.environ.get("OPENVIKING_CLI_CONFIG_FILE")
    or Path.home() / ".openviking" / "ovcli.conf"
)


@pytest.fixture
def live_client(isolate_environment: None) -> Iterator[OvClient]:
    """A client against the configured server, or a skip when there is none."""
    if not os.environ.get(TEST_ROOT_ENV):
        pytest.skip(f"Set {TEST_ROOT_ENV} to a writable viking:// parent to run these.")
    try:
        credentials = load_credentials(REAL_CLI_CONFIG)
    except (FileNotFoundError, ValueError) as exc:  # pragma: no cover - config-dependent
        pytest.skip(f"No OpenViking credentials: {exc}")
    with OvClient(credentials) as client:
        yield client


@pytest.fixture
def live_root(live_client: OvClient) -> Iterator[str]:
    """Create a throwaway directory on the server, and remove it after."""
    parent = os.environ[TEST_ROOT_ENV].rstrip("/")
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
