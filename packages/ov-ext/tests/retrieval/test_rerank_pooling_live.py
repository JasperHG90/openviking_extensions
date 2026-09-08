"""End-to-end proof that pooling reuses the connection, over a real socket.

The unit tests in ``test_rerank.py`` drive the shim with a fake session, which
shows the shim is wired up but not that the fix *works*: the whole claim is
that the rerank client stops opening a TCP connection per call, and only a real
client talking to a real listener can settle that.

So this runs OpenViking's own ``OpenAIRerankClient``, unmodified, against a
local HTTP/1.1 server that counts accepted connections. No Docker and no
network, so it stays in the default suite rather than behind the integration
marker.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from openviking.models.rerank.openai_rerank import OpenAIRerankClient

from ov_ext.retrieval.rerank import install_pooled_rerank, uninstall_pooled_rerank


class _CountingServer(ThreadingHTTPServer):
    """An HTTP server that counts how many TCP connections it accepted."""

    daemon_threads = True
    allow_reuse_address = True

    connections: int = 0
    cookies_seen: list[str]
    traceparents: list[str]

    def process_request(self, request: Any, client_address: Any) -> None:
        """Count one accepted connection, then serve it as usual."""
        self.connections += 1
        super().process_request(request, client_address)


class _RerankHandler(BaseHTTPRequestHandler):
    """Answers any POST with one plausible rerank result per document."""

    # Load-bearing. Under HTTP/1.0 the server closes after every response, the
    # client can reuse nothing, and the pooled and unpooled arms would count
    # the same.
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - the name BaseHTTPRequestHandler wants
        """Reply with a score per document, and record any cookie sent."""
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        server: Any = self.server
        server.cookies_seen.append(self.headers.get("Cookie", ""))
        server.traceparents.append(self.headers.get("traceparent", ""))

        documents = body.get("documents") or body.get("input", {}).get("documents", [])
        payload = json.dumps(
            {
                "results": [
                    {"index": index, "relevance_score": 0.5}
                    for index in range(len(documents))
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # A stickiness cookie, as a load balancer in front of the real service
        # would set. Nothing may replay it; see the cookie test below.
        self.send_header("Set-Cookie", "LB=pinned-to-one-node; Path=/")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Stay quiet; pytest captures enough already."""


@pytest.fixture
def rerank_server() -> Iterator[_CountingServer]:
    """Run the counting server on a free port for one test."""
    server = _CountingServer(("127.0.0.1", 0), _RerankHandler)
    server.cookies_seen = []
    server.traceparents = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def unpatched_between_tests() -> Iterator[None]:
    """Start and finish with OpenViking's own ``requests`` in place."""
    uninstall_pooled_rerank()
    yield
    uninstall_pooled_rerank()


def client_for(server: _CountingServer) -> OpenAIRerankClient:
    """Build a real rerank client pointed at the local server."""
    # `server_address` is typed loosely enough that the host may be bytes;
    # for an AF_INET listener it is always the string form.
    raw_host, port = server.server_address[0], server.server_address[1]
    host = raw_host.decode() if isinstance(raw_host, bytes) else str(raw_host)
    return OpenAIRerankClient(
        api_key="test-key",
        api_base=f"http://{host}:{port}/v1/rerank",
        model_name="test-model",
    )


CALLS = 5


def test_without_pooling_every_call_opens_a_connection(
    rerank_server: _CountingServer,
) -> None:
    """The behaviour being fixed, measured rather than assumed.

    This is the baseline the whole change rests on. If OpenViking ever starts
    pooling on its own, this test fails and the patch becomes dead weight worth
    deleting.
    """
    client = client_for(rerank_server)

    for _ in range(CALLS):
        assert client.rerank_batch("query", ["a document"]) == [0.5]

    assert rerank_server.connections == CALLS


def test_pooling_reuses_one_connection_for_every_call(
    rerank_server: _CountingServer,
) -> None:
    """The fix, measured at the socket."""
    assert install_pooled_rerank() is True
    client = client_for(rerank_server)

    for _ in range(CALLS):
        assert client.rerank_batch("query", ["a document"]) == [0.5]

    assert rerank_server.connections == 1


def test_uninstalling_puts_the_per_call_connection_back(
    rerank_server: _CountingServer,
) -> None:
    """The patch is reversible at the socket too, not just at the attribute."""
    install_pooled_rerank()
    uninstall_pooled_rerank()
    client = client_for(rerank_server)

    for _ in range(CALLS):
        client.rerank_batch("query", ["a document"])

    assert rerank_server.connections == CALLS


def test_the_pooled_session_never_replays_a_cookie(
    rerank_server: _CountingServer,
) -> None:
    """A shared session must not become a stickiness cache.

    The server sets a cookie on every response. A plain ``requests.Session``
    would store it and send it back on call two onward, pinning every rerank in
    the process to one backend node -- silently, and for the life of the
    server. The per-call ``requests.post`` this replaces could not do that,
    so neither may the replacement.
    """
    install_pooled_rerank()
    client = client_for(rerank_server)

    for _ in range(3):
        client.rerank_batch("query", ["a document"])

    assert rerank_server.cookies_seen == ["", "", ""]


def test_more_concurrent_calls_than_the_pool_holds_still_all_succeed(
    rerank_server: _CountingServer,
) -> None:
    """Exceeding ``pool_maxsize`` discards *connections*, never requests.

    urllib3 logs "Connection pool is full, discarding connection" above the
    pool size, which reads alarmingly. What it discards is the socket after the
    response, when the idle pool has no room to keep it -- the request itself
    was created, sent and answered. Above the pool size those calls simply fall
    back to a connection each, which is the behaviour this module improves on
    rather than a failure.

    Pinned with a deliberately tiny pool so the condition is guaranteed rather
    than hoped for.
    """
    install_pooled_rerank(pool_maxsize=2)
    client = client_for(rerank_server)
    concurrent, rounds = 8, 4

    results: list[list[float] | None] = []
    for _ in range(rounds):
        with ThreadPoolExecutor(max_workers=concurrent) as pool:
            results += pool.map(
                lambda _: client.rerank_batch("query", ["a doc"]), range(concurrent)
            )

    calls = concurrent * rounds
    assert results == [[0.5]] * calls, "every call must be answered"
    # The load-bearing half. Four rounds of eight against a pool of two: if
    # exceeding the pool dropped requests, the count above would already be
    # short. If it stopped reuse entirely, this would reach `calls`. Neither
    # happens -- the pool keeps serving two, and the excess opens and closes.
    assert 2 <= rerank_server.connections < calls


def test_pooling_carries_the_trace_context_to_the_service(
    rerank_server: _CountingServer, spans: Any
) -> None:
    """embark stamps trace ids on its spans; this is what makes them ours.

    Asserted against the header the server actually received, not the dict the
    client passed, so it covers the whole path rather than the injection call.
    """
    from ov_ext.observability import _tracer

    install_pooled_rerank()
    client = client_for(rerank_server)

    with _tracer().start_as_current_span("ov_ext.retrieval.retrieve"):
        client.rerank_batch("query", ["a document"])

    assert rerank_server.traceparents, "the server saw no request at all"
    # W3C traceparent is `00-<32 hex trace id>-<16 hex span id>-<flags>`.
    assert rerank_server.traceparents[0].startswith("00-")
