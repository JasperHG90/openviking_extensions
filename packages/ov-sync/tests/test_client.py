"""Tests for the OpenViking HTTP client.

respx intercepts at the transport layer, so the client's own request building,
headers, envelope unwrapping and retry policy all stay under test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from ov_sync.client import (
    MAX_ATTEMPTS,
    BatchWriteOperation,
    NotFoundError,
    OvClient,
    OvError,
    TimeoutOvError,
)
from ov_sync.config import Credentials

BASE = "https://openviking.example/api/v1"


def ok(result: Any) -> httpx.Response:
    """Build a successful OpenViking response envelope."""
    return httpx.Response(200, json={"status": "ok", "result": result, "error": None})


def error(status: int, code: str, message: str) -> httpx.Response:
    """Build a failed OpenViking response envelope."""
    return httpx.Response(
        status,
        json={
            "status": "error",
            "result": None,
            "error": {"code": code, "message": message, "details": None},
        },
    )


@pytest.fixture
def client(credentials: Credentials) -> Iterator[OvClient]:
    """An OvClient pointed at the intercepted base URL."""
    with OvClient(credentials) as open_client:
        yield open_client


@respx.mock
def test_stat_returns_metadata(client: OvClient) -> None:
    """A stat is parsed into the typed result."""
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=ok(
            {"name": "notes", "size": 0, "isDir": True, "modTime": "2026-09-11T00:00:00Z"}
        )
    )

    stat = client.stat("viking://resources/notes")

    assert stat is not None
    assert stat.is_dir is True
    assert stat.name == "notes"


@respx.mock
def test_stat_returns_none_when_absent(client: OvClient) -> None:
    """A missing URI is an answer, not an error."""
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=error(404, "NOT_FOUND", "File not found")
    )

    assert client.stat("viking://resources/gone") is None


@respx.mock
def test_requests_carry_auth_and_identity_headers(client: OvClient) -> None:
    """The API key and the account/user headers go on every request."""
    route = respx.get(f"{BASE}/fs/stat").mock(
        return_value=ok({"name": "n", "size": 0, "isDir": True})
    )

    client.stat("viking://resources/notes")

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["X-OpenViking-Account"] == "acme"
    assert request.headers["X-OpenViking-User"] == "jasper"


@respx.mock
def test_server_error_becomes_ov_error(client: OvClient) -> None:
    """A 400 carries the server's message and code through."""
    respx.post(f"{BASE}/content/batch-write").mock(
        return_value=error(
            400, "INVALID_ARGUMENT", "batch-write root must be a directory"
        )
    )

    with pytest.raises(OvError, match="must be a directory") as caught:
        client.batch_write(
            "viking://resources/notes",
            [BatchWriteOperation(uri="viking://resources/notes/a.md", content="x")],
        )

    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.status_code == 400


@respx.mock
def test_a_surprising_payload_becomes_an_ov_error(client: OvClient) -> None:
    """A ValidationError here would escape every caller's `except OvError`."""
    respx.get(f"{BASE}/fs/stat").mock(return_value=ok(None))

    with pytest.raises(OvError, match="unexpected payload"):
        client.stat("viking://resources/notes")


@pytest.fixture
def instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the backoff so a retry test costs no wall-clock time.

    Tenacity captures its wait strategy when the decorator runs, so patching
    the module-level function has no effect; the Retrying object's own sleep
    is the seam. Each decorated method carries its own, so all three are
    patched rather than the one a test happens to reach first.
    """
    for retrying in (
        OvClient._send_idempotent.retry,  # type: ignore[attr-defined]
        OvClient._send_raw_idempotent.retry,  # type: ignore[attr-defined]
        OvClient._send_write.retry,  # type: ignore[attr-defined]
    ):
        monkeypatch.setattr(retrying, "sleep", lambda _seconds: None)


@respx.mock
def test_transient_error_is_retried(client: OvClient, instant_retries: None) -> None:
    """A 503 is tried again; the second answer is the one returned."""
    route = respx.get(f"{BASE}/fs/stat").mock(
        side_effect=[
            error(503, "UNAVAILABLE", "overloaded"),
            ok({"name": "notes", "size": 0, "isDir": True}),
        ]
    )

    stat = client.stat("viking://resources/notes")

    assert stat is not None
    assert route.call_count == 2


@respx.mock
def test_a_client_error_is_not_retried(client: OvClient) -> None:
    """Repeating a 404 changes nothing, so it is not repeated."""
    route = respx.get(f"{BASE}/fs/stat").mock(
        return_value=error(404, "NOT_FOUND", "File not found")
    )

    client.stat("viking://resources/gone")

    assert route.call_count == 1


@respx.mock
def test_retries_give_up_eventually(client: OvClient, instant_retries: None) -> None:
    """A server that stays down does not hang the run forever."""
    route = respx.get(f"{BASE}/fs/stat").mock(
        return_value=error(503, "UNAVAILABLE", "overloaded")
    )

    with pytest.raises(OvError, match="overloaded"):
        client.stat("viking://resources/notes")

    assert route.call_count == MAX_ATTEMPTS


@respx.mock
def test_retry_honors_retry_after(
    client: OvClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 with Retry-After waits the time the server asked for."""
    slept: list[float] = []
    retrying = OvClient._send_idempotent.retry  # type: ignore[attr-defined]
    monkeypatch.setattr(retrying, "sleep", slept.append)
    respx.get(f"{BASE}/fs/stat").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={
                    "status": "error",
                    "result": None,
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "slow down",
                        "details": None,
                    },
                },
            ),
            ok({"name": "notes", "size": 0, "isDir": True}),
        ]
    )

    client.stat("viking://resources/notes")

    assert slept == [7.0]


@respx.mock
def test_unreachable_server_raises(client: OvClient, instant_retries: None) -> None:
    """A connection failure is reported against the configured URL."""
    respx.get(f"{BASE}/fs/stat").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(OvError, match=r"Cannot reach https://openviking\.example"):
        client.stat("viking://resources/notes")


@respx.mock
def test_batch_write_sends_the_operations(client: OvClient) -> None:
    """The request body carries the root, the mode, and one entry per file."""
    route = respx.post(f"{BASE}/content/batch-write").mock(
        return_value=ok(
            {
                "root_uri": "viking://resources/notes",
                "created": ["viking://resources/notes/a.md"],
                "updated": [],
                "unchanged": [],
            }
        )
    )

    result = client.batch_write(
        "viking://resources/notes",
        [BatchWriteOperation(uri="viking://resources/notes/a.md", content="hello")],
    )

    body = json.loads(route.calls[0].request.content)
    assert body["root_uri"] == "viking://resources/notes"
    assert body["operations"] == [
        {"uri": "viking://resources/notes/a.md", "content": "hello", "mode": "upsert"}
    ]
    assert result.created == ["viking://resources/notes/a.md"]


@respx.mock
def test_batch_write_omits_the_unused_content_field(client: OvClient) -> None:
    """The server forbids sending both content and content_base64."""
    route = respx.post(f"{BASE}/content/batch-write").mock(
        return_value=ok({"root_uri": "r", "created": [], "updated": [], "unchanged": []})
    )

    client.batch_write(
        "viking://resources/notes",
        [
            BatchWriteOperation(
                uri="viking://resources/notes/a.png", content_base64="AAEC"
            )
        ],
    )

    operation = json.loads(route.calls[0].request.content)["operations"][0]
    assert "content" not in operation
    assert operation["content_base64"] == "AAEC"


@respx.mock
def test_rm_reports_whether_anything_was_removed(client: OvClient) -> None:
    """Removing what is already gone is a no-op, not a failure."""
    respx.delete(f"{BASE}/fs").mock(
        side_effect=[ok({"uri": "x"}), error(404, "NOT_FOUND", "File not found")]
    )

    assert client.rm("viking://resources/notes/a.md") is True
    assert client.rm("viking://resources/notes/a.md") is False


@respx.mock
def test_snapshot_commit_is_not_retried(client: OvClient) -> None:
    """A repeated commit is a second commit, so a 503 is not tried again."""
    route = respx.post(f"{BASE}/snapshot/commit").mock(
        return_value=error(503, "UNAVAILABLE", "overloaded")
    )

    with pytest.raises(OvError):
        client.snapshot_commit("before delete")

    assert route.call_count == 1


@respx.mock
def test_not_found_is_its_own_error_type(client: OvClient) -> None:
    """Callers that care about absence catch NotFoundError, not every OvError."""
    respx.post(f"{BASE}/content/batch-write").mock(
        return_value=error(404, "NOT_FOUND", "File not found")
    )

    with pytest.raises(NotFoundError):
        client.batch_write(
            "viking://resources/notes",
            [BatchWriteOperation(uri="viking://resources/notes/a.md", content="x")],
        )


@respx.mock
def test_an_unreadable_body_is_reported_as_such(client: OvClient) -> None:
    """A 200 that is not the expected envelope fails at the boundary."""
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=httpx.Response(200, text="<html>nope</html>")
    )

    with pytest.raises(OvError, match="unreadable body"):
        client.stat("viking://resources/notes")


@respx.mock
@pytest.mark.parametrize("key", ["commit_oid", "commit_id", "commit", "sha"])
def test_snapshot_commit_reads_the_id_under_every_alias(
    client: OvClient, key: str
) -> None:
    """OpenViking 0.4 sends commit_oid; the rest guard against it moving.

    A missed key is silent: the deletion still happens, and the user is left
    without the restore command that undoes it.
    """
    respx.post(f"{BASE}/snapshot/commit").mock(
        return_value=ok({key: "42d45759", "result": "created", "changed": 3})
    )

    assert client.snapshot_commit("before delete").commit_id == "42d45759"


@respx.mock
def test_a_size_refusal_is_not_retried(client: OvClient, instant_retries: None) -> None:
    """RESOURCE_EXHAUSTED arrives as a 429 but is a fact about the request.

    Retrying re-uploads the same oversized body, up to 16 MB a go, and the
    answer cannot change.
    """
    route = respx.post(f"{BASE}/content/batch-write").mock(
        return_value=error(
            429, "RESOURCE_EXHAUSTED", "batch-write total content exceeds size limit"
        )
    )

    with pytest.raises(OvError, match="exceeds size limit"):
        client.batch_write(
            "viking://resources/notes",
            [BatchWriteOperation(uri="viking://resources/notes/a.md", content="x")],
        )

    assert route.call_count == 1


@respx.mock
def test_a_busy_path_lock_is_retried(client: OvClient, instant_retries: None) -> None:
    """A batch-write landing while the last one still holds the tree lock.

    It comes back as CONFLICT / HTTP 409, which reads permanent, but the
    server marks it retryable and it clears as soon as indexing finishes.
    Two syncs in quick succession — a multi-batch run, or watch mode — hit
    this routinely, and without the retry the whole batch is dropped.
    """
    busy = httpx.Response(
        409,
        json={
            "status": "error",
            "result": None,
            "error": {
                "code": "CONFLICT",
                "message": "resource is busy and cannot be written now: viking://x",
                "details": {"conflict_type": "path_busy", "retryable": True},
            },
        },
    )
    route = respx.post(f"{BASE}/content/batch-write").mock(
        side_effect=[
            busy,
            ok({"root_uri": "r", "created": ["r/a.md"], "updated": [], "unchanged": []}),
        ]
    )

    result = client.batch_write(
        "viking://resources/notes",
        [BatchWriteOperation(uri="viking://resources/notes/a.md", content="x")],
    )

    assert route.call_count == 2
    assert result.created == ["r/a.md"]


@respx.mock
def test_a_conflict_the_server_calls_permanent_is_not_retried(
    client: OvClient, instant_retries: None
) -> None:
    """Only the conflicts the server expects to clear are worth repeating."""
    route = respx.post(f"{BASE}/content/batch-write").mock(
        return_value=httpx.Response(
            409,
            json={
                "status": "error",
                "result": None,
                "error": {
                    "code": "CONFLICT",
                    "message": "already exists",
                    "details": {"retryable": False},
                },
            },
        )
    )

    with pytest.raises(OvError, match="already exists"):
        client.batch_write(
            "viking://resources/notes",
            [BatchWriteOperation(uri="viking://resources/notes/a.md", content="x")],
        )

    assert route.call_count == 1


@respx.mock
def test_a_null_details_field_keeps_the_error_code(client: OvClient) -> None:
    """The server sends `"details": null` for errors that carry none.

    Rejecting it fails the whole envelope, and the fallback path drops the
    code — so every such error would arrive codeless and be classified wrong.
    """
    respx.get(f"{BASE}/fs/stat").mock(
        return_value=httpx.Response(
            400,
            json={
                "status": "error",
                "result": None,
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "bad uri",
                    "details": None,
                },
            },
        )
    )

    with pytest.raises(OvError, match="bad uri") as caught:
        client.stat("viking://nope")

    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.details == {}


@respx.mock
def test_mkdir_tolerates_a_directory_that_already_exists(client: OvClient) -> None:
    """The server reports that as CONFLICT, never as ALREADY_EXISTS.

    It matters on a retry: ov-sync creates a root with a description, so the
    server writes a sidecar and embeds it, which is slow enough to time out.
    The retry then finds the directory its own lost first attempt created.
    """
    respx.post(f"{BASE}/fs/mkdir").mock(
        return_value=httpx.Response(
            409,
            json={
                "status": "error",
                "result": None,
                "error": {
                    "code": "CONFLICT",
                    "message": "already exists: /lab/resources/notes",
                    "details": {"resource": "viking://resources/notes"},
                },
            },
        )
    )

    client.mkdir("viking://resources/notes")


@respx.mock
def test_mkdir_does_not_swallow_a_busy_lock(
    client: OvClient, instant_retries: None
) -> None:
    """A busy tree is the other thing CONFLICT means, and it is not success."""
    respx.post(f"{BASE}/fs/mkdir").mock(
        return_value=httpx.Response(
            409,
            json={
                "status": "error",
                "result": None,
                "error": {
                    "code": "CONFLICT",
                    "message": "resource is busy",
                    "details": {"conflict_type": "path_busy", "retryable": True},
                },
            },
        )
    )

    with pytest.raises(OvError, match="busy"):
        client.mkdir("viking://resources/notes")


@respx.mock
def test_a_batch_write_timeout_is_not_retried(
    client: OvClient, instant_retries: None
) -> None:
    """A write that stopped answering may have landed, so it is not repeated.

    Repeating it re-uploads every file and asks for the batch to be indexed
    again, which is more of the load that ran the clock out in the first place.
    """
    route = respx.post(f"{BASE}/content/batch-write").mock(
        side_effect=httpx.ReadTimeout("read timed out")
    )

    with pytest.raises(TimeoutOvError, match="timed out"):
        client.batch_write(
            "viking://resources/notes",
            [BatchWriteOperation(uri="viking://resources/notes/a.md", content="a")],
        )

    assert route.call_count == 1


@respx.mock
def test_a_batch_write_refusal_is_still_retried(
    client: OvClient, instant_retries: None
) -> None:
    """A 503 means the server never took the batch, so sending it again is free."""
    route = respx.post(f"{BASE}/content/batch-write").mock(
        side_effect=[
            error(503, "UNAVAILABLE", "overloaded"),
            ok({"root_uri": "viking://resources/notes", "created": ["a"], "updated": []}),
        ]
    )

    written = client.batch_write(
        "viking://resources/notes",
        [BatchWriteOperation(uri="viking://resources/notes/a.md", content="a")],
    )

    assert written.created == ["a"]
    assert route.call_count == 2


@respx.mock
def test_a_read_timeout_is_still_retried(client: OvClient, instant_retries: None) -> None:
    """Only writes are held back. Asking again costs the server nothing."""
    route = respx.get(f"{BASE}/fs/stat").mock(
        side_effect=[
            httpx.ReadTimeout("read timed out"),
            ok({"name": "notes", "size": 0, "isDir": True}),
        ]
    )

    assert client.stat("viking://resources/notes") is not None
    assert route.call_count == 2


@respx.mock
def test_tree_returns_the_files_under_a_root(client: OvClient) -> None:
    """Directories are dropped; files come back with their path below the root."""
    respx.get(f"{BASE}/fs/tree").mock(
        return_value=ok(
            [
                {"rel_path": "sub", "size": 0, "isDir": True, "modTime": None},
                {
                    "rel_path": "sub/a.md",
                    "size": 3,
                    "isDir": False,
                    "modTime": "2026-09-16T11:46:42Z",
                },
            ]
        )
    )

    entries = client.tree("viking://resources/notes")

    assert [entry.rel_path for entry in entries] == ["sub/a.md"]
    assert entries[0].size == 3


@respx.mock
def test_tree_asks_for_hidden_entries(client: OvClient) -> None:
    """A folder of dotfiles is still a folder of files."""
    route = respx.get(f"{BASE}/fs/tree").mock(return_value=ok([]))

    client.tree("viking://resources/notes")

    assert route.calls[0].request.url.params["show_all_hidden"] == "true"


@respx.mock
def test_tree_refuses_a_listing_at_the_node_ceiling(client: OvClient) -> None:
    """A truncated listing looks exactly like a small tree, so it is refused.

    Taking it at face value would report every file past the cut as absent,
    and a caller would upload the lot.
    """
    respx.get(f"{BASE}/fs/tree").mock(
        return_value=ok(
            [{"rel_path": f"{i}.md", "size": 1, "isDir": False} for i in range(4)]
        )
    )

    with pytest.raises(OvError, match="incomplete"):
        client.tree("viking://resources/notes", node_limit=4)


@respx.mock
def test_tree_refuses_a_listing_at_the_depth_ceiling(client: OvClient) -> None:
    """Same reasoning for a walk that stopped going down."""
    respx.get(f"{BASE}/fs/tree").mock(
        return_value=ok([{"rel_path": "a/b/c.md", "size": 1, "isDir": False}])
    )

    with pytest.raises(OvError, match="deeper is missing"):
        client.tree("viking://resources/notes", level_limit=3)


@respx.mock
def test_download_returns_the_stored_bytes(client: OvClient) -> None:
    """The body comes back verbatim, which is what makes it worth hashing."""
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n raw")
    )

    assert client.download("viking://resources/notes/a.png") == b"\x89PNG\r\n\x1a\n raw"


@respx.mock
def test_download_returns_none_when_absent(client: OvClient) -> None:
    """Nothing stored is an answer, not a failure."""
    respx.get(f"{BASE}/content/download").mock(
        return_value=error(404, "NOT_FOUND", "File not found")
    )

    assert client.download("viking://resources/notes/gone.md") is None


@respx.mock
def test_a_gateway_timeout_is_not_resent(client: OvClient, instant_retries: None) -> None:
    """504 is a read timeout with a status code: the backend took the request.

    It reaches the retry predicate as a TransientOvError like any other 5xx, so
    without the classification the batch would be uploaded four times.
    """
    route = respx.post(f"{BASE}/content/batch-write").mock(
        return_value=error(504, "TIMEOUT", "gateway timeout")
    )

    with pytest.raises(TimeoutOvError, match="gateway timeout"):
        client.batch_write(
            "viking://resources/notes",
            [BatchWriteOperation(uri="viking://resources/notes/a.md", content="a")],
        )

    assert route.call_count == 1


@respx.mock
def test_a_service_unavailable_is_still_resent(
    client: OvClient, instant_retries: None
) -> None:
    """503 is the proxy saying it had no backend to hand the request to."""
    route = respx.post(f"{BASE}/content/batch-write").mock(
        side_effect=[
            error(503, "UNAVAILABLE", "no server is available"),
            ok({"root_uri": "viking://resources/notes", "created": ["a"], "updated": []}),
        ]
    )

    client.batch_write(
        "viking://resources/notes",
        [BatchWriteOperation(uri="viking://resources/notes/a.md", content="a")],
    )

    assert route.call_count == 2


@respx.mock
def test_a_gateway_timeout_on_a_read_is_still_retried(
    client: OvClient, instant_retries: None
) -> None:
    """Only writes are held back; asking again costs the server nothing."""
    route = respx.get(f"{BASE}/fs/stat").mock(
        side_effect=[
            error(504, "TIMEOUT", "gateway timeout"),
            ok({"name": "notes", "size": 0, "isDir": True}),
        ]
    )

    assert client.stat("viking://resources/notes") is not None
    assert route.call_count == 2


@respx.mock
def test_tree_refuses_an_entry_it_cannot_parse(client: OvClient) -> None:
    """Dropping them would turn a changed payload into an empty listing.

    An empty listing reads as "the server holds nothing", which a caller
    answers by uploading everything while reporting a number that looks true.
    """
    respx.get(f"{BASE}/fs/tree").mock(return_value=ok(["a.md", "b.md"]))

    with pytest.raises(OvError, match="unexpected payload"):
        client.tree("viking://resources/notes")


@respx.mock
def test_download_rejects_an_error_hidden_in_a_200(client: OvClient) -> None:
    """Handing those bytes back would pass an error message off as the file."""
    respx.get(f"{BASE}/content/download").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "error",
                "result": None,
                "error": {"code": "INTERNAL", "message": "read failed", "details": None},
            },
        )
    )

    with pytest.raises(OvError, match="read failed"):
        client.download("viking://resources/notes/a.md")


@respx.mock
def test_a_long_read_timeout_leaves_connect_short(client: OvClient) -> None:
    """A batch-write waits ten minutes for an answer, not to reach the server.

    A bare float sets every phase, so an unreachable host would hang for the
    length of a batch-write instead of failing in seconds.
    """
    route = respx.post(f"{BASE}/content/batch-write").mock(
        return_value=ok({"root_uri": "viking://resources/notes", "created": []})
    )

    client.batch_write(
        "viking://resources/notes",
        [BatchWriteOperation(uri="viking://resources/notes/a.md", content="a")],
        timeout=600.0,
    )

    timeout = route.calls[0].request.extensions["timeout"]
    assert timeout["read"] == 600.0
    assert timeout["connect"] == 10.0
