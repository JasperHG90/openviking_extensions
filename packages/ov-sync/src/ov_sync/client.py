"""HTTP client for the handful of OpenViking endpoints a sync needs.

The `ov` CLI has no batch-write command, and batch-write is the whole reason
this is fast: one request carries up to 256 files, and the server reindexes
once for the batch instead of once per file. So ov-sync talks to the REST API
directly, reusing the CLI's credentials.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Literal, TypeVar

import httpx
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import Credentials

# 429 plus the gateway and overload codes. A 400 or 404 is the server telling
# you something true about the request, and repeating it changes nothing.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# OpenViking answers 429 for two unrelated things: a rate limit, which passes,
# and RESOURCE_EXHAUSTED, which is how it refuses a batch that is too large or
# too long. The second is a fact about the request, so retrying it just uploads
# the same oversized body four times.
PERMANENT_CODES = frozenset({"RESOURCE_EXHAUSTED"})

MAX_ATTEMPTS = 4


def _is_retryable(response: httpx.Response) -> bool:
    """Whether a failed request is worth sending again.

    The status alone is not enough in either direction. A busy path lock — one
    batch-write arriving while the last one's indexing still holds the tree —
    comes back as ``CONFLICT``, HTTP 409, which reads permanent and is not:
    the server marks it ``retryable`` in the error details, and says so for
    every conflict it expects to clear on its own. Meanwhile a 429 can be a
    rate limit or a refusal to accept an oversized batch, and only one of
    those changes on a second try.

    Parameters
    ----------
    response :
        The failed response.

    Returns
    -------
    bool
        True when the request should be retried.
    """
    error = _parse_error(response)
    if error.code in PERMANENT_CODES:
        return False
    if error.details.get("retryable") is True:
        return True
    return response.status_code in RETRYABLE_STATUS


def _is_already_there(exc: OvError) -> bool:
    """Whether a failed mkdir failed because the directory is already there.

    OpenViking reports that as ``CONFLICT``, the same code it uses for a busy
    path lock — and never as ``ALREADY_EXISTS``, whatever the name suggests.
    The two are told apart by the ``retryable`` flag, which a busy lock always
    sets and an already-exists never does.

    This matters on a retry, not a first call: ov-sync creates a root with a
    description, so the server writes a sidecar and embeds it, which is slow
    enough to time out. The retry then finds the directory its own lost first
    attempt created, and without this the user is told creation failed.
    """
    if exc.code != "CONFLICT":
        return False
    return not exc.details.get("retryable", False)


class OvError(Exception):
    """An error the OpenViking server reported.

    Parameters
    ----------
    message :
        Human-readable description, taken from the server's error envelope
        when it sent one.
    code :
        The server's error code, e.g. ``NOT_FOUND``.
    status_code :
        HTTP status, or None when the request never got a response.
    details :
        The error's structured context. One code covers several situations —
        ``CONFLICT`` is both a busy path lock and an already-existing
        directory — and this is what tells them apart.

    Attributes
    ----------
    code : str
        The server's error code.
    status_code : int or None
        HTTP status of the failed response.
    details : dict[str, Any]
        The error's structured context, empty when the server sent none.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class TransientOvError(OvError):
    """A server error worth retrying: rate limiting, or an overloaded backend.

    Attributes
    ----------
    retry_after : float or None
        Seconds the server asked the caller to wait, from ``Retry-After``.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, code=code, status_code=status_code, details=details)
        self.retry_after = retry_after


class NotFoundError(OvError):
    """The requested URI does not exist."""


class ApiError(BaseModel):
    """The ``error`` object in an OpenViking response envelope."""

    code: str = Field(default="", description="Machine-readable error code.")
    message: str = Field(default="", description="Human-readable description.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Error-specific context. One code covers several cases, "
        "so this is often what tells them apart.",
    )

    @field_validator("details", mode="before")
    @classmethod
    def _absent_details_are_empty(cls, value: object) -> object:
        """Treat a null ``details`` as no details.

        The server sends ``"details": null`` for errors that carry none.
        Rejecting that would fail the whole envelope, and the fallback path
        drops the error code — so every such error would arrive codeless and
        be classified wrong.
        """
        return {} if value is None else value


class ApiResponse(BaseModel):
    """The envelope every OpenViking endpoint returns."""

    status: str = Field(description='"ok" or "error".')
    result: Any = Field(default=None, description="Endpoint-specific payload.")
    error: ApiError | None = Field(default=None, description="Set when status is error.")


class StatResult(BaseModel):
    """Metadata for one resource, as returned by ``/fs/stat``."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(description="Final path component.")
    size: int = Field(description="Size in bytes; 0 for a directory.")
    is_dir: bool = Field(
        alias="isDir", description="Whether the resource is a directory."
    )
    mod_time: str | None = Field(
        default=None, alias="modTime", description="Last modification, ISO 8601."
    )


class BatchWriteResult(BaseModel):
    """What a batch-write did, per target URI."""

    root_uri: str = Field(description="Directory the batch was written under.")
    created: list[str] = Field(
        default_factory=list, description="URIs that did not exist before."
    )
    updated: list[str] = Field(
        default_factory=list, description="URIs whose content was replaced."
    )
    unchanged: list[str] = Field(
        default_factory=list, description="URIs the server left alone."
    )

    @property
    def written(self) -> list[str]:
        """Every URI the batch touched."""
        return self.created + self.updated + self.unchanged


class SnapshotCommitResult(BaseModel):
    """A workspace snapshot, as returned by ``/snapshot/commit``.

    OpenViking 0.4 returns the identifier as ``commit_oid``. The other aliases
    are there because the payload is not pinned by a published schema, and an
    id read under the wrong key would cost the user their undo instructions
    while the deletion went ahead regardless.
    """

    model_config = ConfigDict(extra="allow")

    commit_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "commit_oid", "commit_id", "commit", "id", "sha", "revision"
        ),
        description="Identifier to pass to `ov snapshot restore`.",
    )


class BatchWriteOperation(BaseModel):
    """One file in a batch-write request.

    Exactly one of ``content`` and ``content_base64`` is set: text goes as
    text, and anything that is not valid UTF-8 goes as base64.
    """

    uri: str = Field(description="Target URI, which must sit under the batch root.")
    content: str | None = Field(default=None, description="UTF-8 text to write.")
    content_base64: str | None = Field(
        default=None, description="Base64-encoded bytes to write."
    )
    mode: Literal["replace", "append", "create", "upsert"] = Field(
        default="upsert",
        description="upsert creates the file when absent and replaces it when present.",
    )


def _retry_wait(retry_state: RetryCallState) -> float:
    """Wait for the server's ``Retry-After`` when it sent one, else back off.

    Parameters
    ----------
    retry_state :
        Tenacity's state for the attempt that just failed.

    Returns
    -------
    float
        Seconds to wait before the next attempt.
    """
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if isinstance(exc, TransientOvError) and exc.retry_after is not None:
        return exc.retry_after
    fallback: float = wait_exponential_jitter(initial=1.0, max=30.0)(retry_state)
    return fallback


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header given in seconds, if it is usable."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        # The header also permits an HTTP date. Backing off exponentially is a
        # fine answer to one of those, so only the numeric form is read.
        return max(0.0, float(raw))
    except ValueError:
        return None


class OvClient:
    """Talks to an OpenViking server over its REST API.

    Use it as a context manager so the connection pool is closed:

    >>> with OvClient(credentials) as client:  # doctest: +SKIP
    ...     client.stat("viking://resources/notes")

    Parameters
    ----------
    credentials :
        Server URL, API key, and identity headers.
    timeout :
        Per-phase timeouts. The default allows a long read because indexing
        happens inside a batch-write request, while keeping connect short so
        an unreachable server fails quickly.
    """

    def __init__(
        self,
        credentials: Credentials,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._credentials = credentials
        headers = {"Authorization": f"Bearer {credentials.api_key}"}
        if credentials.account:
            headers["X-OpenViking-Account"] = credentials.account
        if credentials.user:
            headers["X-OpenViking-User"] = credentials.user
        self._client = httpx.Client(
            base_url=f"{credentials.url}/api/v1",
            headers=headers,
            timeout=timeout
            or httpx.Timeout(connect=10.0, read=300.0, write=120.0, pool=10.0),
        )

    def __enter__(self) -> OvClient:
        """Return the open client, for use in a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection pool on the way out of a ``with`` block."""
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> object:
        """Make one request and unwrap the response envelope.

        Parameters
        ----------
        method :
            HTTP method.
        path :
            Path below ``/api/v1``.
        params :
            Query parameters.
        json :
            Request body.
        timeout :
            Read timeout override, in seconds, for this call alone.

        Returns
        -------
        object
            The envelope's ``result`` field, unvalidated. Typed as ``object``
            rather than ``Any`` so a caller cannot use it without parsing it
            into a model first.

        Raises
        ------
        TransientOvError
            On a rate limit or a 5xx, and on a connection or timeout failure.
        NotFoundError
            On a 404.
        OvError
            On any other error response, or an unreadable body.
        """
        request_timeout = (
            httpx.USE_CLIENT_DEFAULT if timeout is None else httpx.Timeout(timeout)
        )
        try:
            response = self._client.request(
                method, path, params=params, json=json, timeout=request_timeout
            )
        except httpx.TimeoutException as exc:
            raise TransientOvError(f"{method} {path} timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientOvError(
                f"Cannot reach {self._credentials.url}: {exc}"
            ) from exc

        if response.status_code >= 400 and _is_retryable(response):
            error = _parse_error(response)
            raise TransientOvError(
                error.message or f"{method} {path} failed with {response.status_code}",
                code=error.code,
                status_code=response.status_code,
                details=error.details,
                retry_after=_retry_after_seconds(response),
            )
        if response.status_code >= 400:
            error = _parse_error(response)
            message = (
                error.message or f"{method} {path} failed with {response.status_code}"
            )
            if response.status_code == 404:
                raise NotFoundError(
                    message, code=error.code, status_code=404, details=error.details
                )
            raise OvError(
                message,
                code=error.code,
                status_code=response.status_code,
                details=error.details,
            )

        try:
            envelope = ApiResponse.model_validate_json(response.text)
        except ValueError as exc:
            raise OvError(f"{method} {path} returned an unreadable body: {exc}") from exc
        if envelope.status != "ok":
            error = envelope.error or ApiError()
            raise OvError(
                error.message or f"{method} {path} failed",
                code=error.code,
                details=error.details,
            )
        return envelope.result

    @retry(
        retry=retry_if_exception_type(TransientOvError),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=_retry_wait,
        reraise=True,
    )
    def _send_idempotent(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> object:
        """Send a request that can safely be repeated, retrying transient failures.

        Every call routed through here either changes nothing (a read) or
        converges on the same end state when repeated (an upsert, an mkdir,
        a delete). Snapshot commits do not, and go through :meth:`_send`.
        """
        return self._send(method, path, params=params, json=json, timeout=timeout)

    def stat(self, uri: str) -> StatResult | None:
        """Return metadata for a URI, or None when nothing is there.

        Parameters
        ----------
        uri :
            The viking:// URI to look up.

        Returns
        -------
        StatResult or None
            The resource's metadata, or None if it does not exist.
        """
        try:
            result = self._send_idempotent("GET", "/fs/stat", params={"uri": uri})
        except NotFoundError:
            return None
        return _parse_result(StatResult, result, "/fs/stat")

    def mkdir(self, uri: str, description: str | None = None) -> None:
        """Create a directory, and any parents it needs.

        Only the sync *root* ever needs this: batch-write creates the
        directories below it as a side effect of writing the files.

        Parameters
        ----------
        uri :
            Directory URI to create.
        description :
            Optional description stored on the directory.

        Raises
        ------
        OvError
            If the directory cannot be created. An already-existing directory
            is not an error: it is the state the caller wanted.
        """
        body: dict[str, Any] = {"uri": uri}
        if description:
            body["description"] = description
        try:
            self._send_idempotent("POST", "/fs/mkdir", json=body)
        except OvError as exc:
            if _is_already_there(exc):
                return
            raise

    def batch_write(
        self,
        root_uri: str,
        operations: list[BatchWriteOperation],
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> BatchWriteResult:
        """Write a batch of files under one directory and reindex them once.

        Parameters
        ----------
        root_uri :
            An existing directory that contains every target.
        operations :
            The files to write, at most 256 of them, 8 MB each and 16 MB in
            total. Splitting a larger set is the caller's job.
        wait :
            Wait for the server to finish reindexing before returning.
        timeout :
            Read timeout for this request, in seconds.

        Returns
        -------
        BatchWriteResult
            Which URIs were created, updated, and left unchanged.

        Raises
        ------
        OvError
            If any write in the batch fails. The server stops at the first
            failure and does not report which earlier writes landed, so the
            caller must treat the whole batch as unrecorded and let the next
            run upsert it again.
        """
        body = {
            "root_uri": root_uri,
            "operations": [op.model_dump(exclude_none=True) for op in operations],
            "wait": wait,
        }
        result = self._send_idempotent(
            "POST", "/content/batch-write", json=body, timeout=timeout
        )
        return _parse_result(BatchWriteResult, result, "/content/batch-write")

    def rm(self, uri: str, *, recursive: bool = False, wait: bool = False) -> bool:
        """Remove a resource.

        Parameters
        ----------
        uri :
            Resource to remove.
        recursive :
            Remove a directory and everything under it.
        wait :
            Wait for the semantic index to catch up.

        Returns
        -------
        bool
            True if something was removed, False if it was already gone.
        """
        try:
            self._send_idempotent(
                "DELETE",
                "/fs",
                params={"uri": uri, "recursive": recursive, "wait": wait},
            )
        except NotFoundError:
            return False
        return True

    def snapshot_commit(
        self, message: str, paths: list[str] | None = None
    ) -> SnapshotCommitResult:
        """Commit a workspace snapshot, so a later mistake can be restored.

        Not retried: a repeated commit is a second commit, not the same one.

        Parameters
        ----------
        message :
            Commit message.
        paths :
            Limit the snapshot to these URIs. Defaults to the whole workspace.

        Returns
        -------
        SnapshotCommitResult
            The new commit, whose id is what ``ov snapshot restore`` takes.
        """
        body: dict[str, Any] = {"message": message}
        if paths:
            body["paths"] = paths
        result = self._send("POST", "/snapshot/commit", json=body)
        return SnapshotCommitResult.model_validate(
            result if isinstance(result, dict) else {}
        )


ModelT = TypeVar("ModelT", bound=BaseModel)


def _parse_result(model: type[ModelT], result: object, path: str) -> ModelT:
    """Validate an endpoint's payload, reporting a surprise as an OvError.

    Without this, a 200 whose ``result`` is null or reshaped raises
    ``pydantic.ValidationError``, which every caller's ``except OvError``
    misses — the user gets a traceback where they should get one line saying
    the server answered oddly.

    Parameters
    ----------
    model :
        The model the payload should match.
    result :
        The envelope's ``result`` field.
    path :
        Endpoint path, for the error message.

    Returns
    -------
    ModelT
        The validated payload.

    Raises
    ------
    OvError
        If the payload is not the documented shape.
    """
    try:
        return model.model_validate(result)
    except ValidationError as exc:
        raise OvError(f"{path} returned an unexpected payload: {exc}") from exc


def _parse_error(response: httpx.Response) -> ApiError:
    """Read the error object out of a failed response, falling back to its text."""
    try:
        envelope = ApiResponse.model_validate_json(response.text)
    except ValueError:
        return ApiError(message=response.text.strip()[:500])
    return envelope.error or ApiError(message=response.text.strip()[:500])
