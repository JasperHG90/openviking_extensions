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
    is the seam.
    """
    retrying = OvClient._send_idempotent.retry  # type: ignore[attr-defined]
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
