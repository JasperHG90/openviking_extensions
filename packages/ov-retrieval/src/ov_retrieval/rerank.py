"""Give OpenViking's rerank client one pooled connection and a trace parent.

OpenViking reranks with a module-level ``requests.post``
(``openviking/models/rerank/openai_rerank.py``), which builds a fresh
connection per call. That is cheap when a search reranks once. It does not:
hierarchical descent calls ``_rerank_scores`` once per directory it visits, so
one search opens hundreds of connections and pays a TCP and TLS handshake on
every one. Measured against a LAN rerank service, the handshake costs more than
the inference it wraps -- calls the service answers in about 5ms arrive back in
15 to 30ms.

Swapping the module's ``requests`` reference for a shim over a single
``requests.Session`` keeps the connection open across calls. The shim takes the
same arguments and returns the same ``requests.Response``, so the client's own
error handling and response parsing are untouched.

One thing a ``Session`` does change, and it is changed back here: it keeps a
cookie jar and replays it on every later call, where a per-call
``requests.post`` had nowhere to keep one. A load balancer in front of the
rerank service hands out a stickiness cookie, and replaying it would pin every
rerank call in the process to a single backend node -- quietly, and for the
lifetime of the server. The jar is refused rather than shared.

The swap is also where a ``traceparent`` header goes. embark already emits
``trace_id`` and ``span_id`` on its own spans, but a bare ``requests.post``
sends no context, so those spans land in a trace of their own rather than under
the retrieval that caused them. Injecting the current context joins them up.

Injecting after the VikingDB client has signed its headers is safe, which is
worth writing down because it looks like it should not be. ``SignerV4`` signs
``Content-Type``, ``Content-Md5``, ``Host`` and anything starting with ``X-``,
and names exactly that set in the request's ``SignedHeaders``. A verifier
checks the headers named there and ignores the rest, so ``traceparent`` --
which can never be in the set -- rides along without disturbing the signature.
``requests`` already puts five unsigned headers on every one of those calls
(``Accept``, ``Accept-Encoding``, ``Connection``, ``Content-Length``,
``User-Agent``), so a service refusing unsigned headers would have rejected
this client long before we added a sixth.

One thing to know before adding to what goes out: ``inject`` emits whatever
propagators are configured, which by default is ``tracecontext`` *and*
``baggage``. Nothing in OpenViking sets baggage today, but if anything ever
does, its contents would leave the process with every rerank call -- and the
rerank endpoint is frequently a third-party vendor. Pin ``OTEL_PROPAGATORS`` to
``tracecontext`` if that matters more than the convenience.

Why ``requests`` here and not ``httpx``, against this repo's usual rule: this
module is a drop-in for an existing ``requests`` call site inside somebody
else's library. Its caller was written against ``requests`` semantics --
response type, proxy and certificate handling, environment variables -- and
substituting a different client would change all of them to save nothing. The
rule is about the HTTP calls we write, and this is a connection pool for the
ones we did not.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import MutableMapping
from http import cookiejar
from types import ModuleType
from typing import Any

import requests
from opentelemetry.propagate import inject

from .observability import annotate, traced_sync

__all__ = ["install_pooled_rerank", "uninstall_pooled_rerank"]

logger = logging.getLogger(__name__)

# Rerank clients that reach the network through a module-level `requests`.
# Both are patched, since which one runs depends on the configured provider.
# They do not use the same verb: the OpenAI-shaped client calls `post`, the
# VikingDB one calls `request`, so the shim implements both.
_MODULES = (
    "openviking.models.rerank.openai_rerank",
    "openviking.models.rerank.volcengine_rerank",
)

# What each patched module had before, so uninstall() can put it back. Module
# state because the patch is process-global, exactly like `install()`'s.
_original: dict[str, Any] = {}

# The session the shims share, kept so uninstall() can close its sockets.
_session: requests.Session | None = None


class _NoCookies(cookiejar.CookiePolicy):
    """A cookie policy that keeps the session's jar permanently empty.

    Installed on the shared session so it cannot become a stickiness cache.
    See this module's docstring for why that matters.

    ``set_ok`` is the method that does the work, and it is the only one worth
    reading. The three ``*_return_ok`` methods below are required by the
    ``CookiePolicy`` interface but are never consulted for an outgoing request:
    ``Session.prepare_request`` merges into a *fresh* jar carrying the default
    policy, so nothing asks this one what may be sent. They are implemented
    consistently rather than left to raise, but an empty jar is what actually
    guarantees no cookie goes out.
    """

    def set_ok(self, cookie: Any, request: Any) -> bool:
        """Refuse every cookie the rerank service tries to set.

        The whole guarantee rests here: a jar that never accepts anything has
        nothing to replay.
        """
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        """Refuse to send a cookie back. Not consulted; see the class docstring."""
        return False

    def domain_return_ok(self, domain: str, request: Any) -> bool:
        """Refuse the per-domain lookup. Not consulted; see the class docstring."""
        return False

    def path_return_ok(self, path: str, request: Any) -> bool:
        """Refuse the per-path lookup. Not consulted; see the class docstring."""
        return False

    # Read off the policy when a jar extracts cookies from a response, which
    # is the path `set_ok` sits on.
    netscape = True
    rfc2965 = False
    hide_cookie2 = False


def _document_count(body: object) -> int | None:
    """Return how many documents a rerank request body carries, if it says.

    Two shapes reach here. The OpenAI-shaped body puts ``documents`` at the top
    level; a DashScope-native endpoint nests it under ``input``. Anything else
    returns ``None`` rather than a wrong number.
    """
    if not isinstance(body, dict):
        return None
    documents = body.get("documents")
    if documents is None:
        nested = body.get("input")
        if isinstance(nested, dict):
            documents = nested.get("documents")
    return len(documents) if isinstance(documents, list) else None


class _PooledRequests:
    """Stands in for the ``requests`` module, over one pooled session.

    ``post`` and ``request`` are implemented, which is what the two rerank
    clients call -- the OpenAI-shaped one posts, the VikingDB one goes through
    ``request``. Implementing only the first left the second opening a
    connection per call while the log said it was pooled. An attribute the
    clients might reach for later falls through to the real module rather than
    raising, so this cannot silently break a code path it was not written for.

    Parameters
    ----------
    session :
        The session whose connection pool every call shares.
    real :
        The genuine ``requests`` module, for anything not implemented here.
    """

    def __init__(self, session: requests.Session, real: ModuleType) -> None:
        self._session = session
        self._real = real

    @traced_sync("ov_retrieval.rerank_call")
    def post(self, *args: Any, **kwargs: Any) -> requests.Response:
        """Post through the pooled session, carrying the current trace context.

        Returns
        -------
        requests.Response
            Exactly what ``requests.post`` would have returned.
        """
        self._prepare(kwargs)
        return self._session.post(*args, **kwargs)

    @traced_sync("ov_retrieval.rerank_call")
    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        """Send through the pooled session, carrying the current trace context.

        The VikingDB client reaches the network this way rather than through
        ``post``, so without this its calls would fall through to the real
        module and open a connection each -- while the log said they were
        pooled.

        Returns
        -------
        requests.Response
            Exactly what ``requests.request`` would have returned.
        """
        self._prepare(kwargs)
        return self._session.request(*args, **kwargs)

    @staticmethod
    def _prepare(kwargs: dict[str, Any]) -> None:
        """Stamp the trace context on the outgoing headers, and note the batch.

        Any mutable mapping is accepted rather than only ``dict``. Both clients
        happen to pass a plain dictionary today; nothing here controls that,
        and a header container is the sort of thing a library swaps for a
        case-insensitive mapping without calling it a breaking change.
        """
        headers = kwargs.get("headers")
        if isinstance(headers, MutableMapping):
            # W3C `traceparent`, so the rerank service's own spans become
            # children of this one rather than roots of their own trace.
            inject(headers)
        count = _document_count(kwargs.get("json"))
        if count is not None:
            annotate({"ov_retrieval.documents": count})

    def __getattr__(self, name: str) -> Any:
        """Fall through to the real ``requests`` module."""
        return getattr(self._real, name)


def install_pooled_rerank(*, pool_maxsize: int = 32) -> bool:
    """Route the rerank clients through one pooled, trace-propagating session.

    Call once at startup, before the first search. Calling again is a no-op
    rather than a second layer.

    Parameters
    ----------
    pool_maxsize :
        Connections the pool keeps to the rerank host, sized to roughly what
        ``asyncio.to_thread``'s default executor can have in flight. Exceeding
        it costs reuse, not requests: urllib3 opens the extra connections and
        closes them after the response rather than keeping them, so those calls
        fall back to the per-call behaviour this function exists to avoid.

    Returns
    -------
    bool
        Whether anything was patched. False when the modules are absent, which
        is not an error: a deployment with no OpenAI-shaped rerank provider has
        nothing here to pool, and retrieval works either way.
    """
    global _session

    if _original:
        logger.debug("ov-retrieval: rerank pooling already installed")
        return False

    session = requests.Session()
    # Refused, not merely emptied: see this module's docstring. A shared jar
    # would replay a load balancer's stickiness cookie on every later call.
    session.cookies.set_policy(_NoCookies())
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_maxsize, pool_maxsize=pool_maxsize
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    patched = False
    for name in _MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            # A provider this OpenViking build does not ship. Skipped rather
            # than raised: the other module may still be there, and reranking
            # works unpooled regardless.
            logger.debug("ov-retrieval: %s absent; not pooling it", name)
            continue
        real = getattr(module, "requests", None)
        if real is None:
            logger.debug("ov-retrieval: %s has no `requests` to patch", name)
            continue
        _original[name] = real
        module.requests = _PooledRequests(session, real)  # type: ignore[attr-defined]
        patched = True

    if patched:
        _session = session
        logger.info(
            "ov-retrieval: rerank calls now pooled for %s",
            ", ".join(sorted(_original)),
        )
    else:
        # Nothing took the patch, so the session would only leak.
        session.close()
    return patched


def uninstall_pooled_rerank() -> None:
    """Put the real ``requests`` module back on every patched rerank client.

    Closes the pooled session too. Without that, an install/uninstall/install
    cycle -- which is what a test suite does -- leaves the first session's
    sockets open with nothing referencing them.

    Exists mostly so a test can undo the patch: it is process-global state, and
    a test that installs without restoring changes every test after it. Does
    nothing when nothing was installed.
    """
    global _session

    for name, real in _original.items():
        try:
            module = importlib.import_module(name)
        except ImportError:  # pragma: no cover - it imported a moment ago
            continue
        module.requests = real  # type: ignore[attr-defined]
    _original.clear()
    if _session is not None:
        _session.close()
        _session = None
