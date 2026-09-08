"""OpenTelemetry spans for hybrid retrieval.

There is nothing to configure here, which is the point. OpenViking's server
installs a process-global ``TracerProvider`` at startup -- ``app.py`` calls
``init_tracer_from_server_config`` from ``server.observability.traces`` -- and
:func:`ov_retrieval.install` patches the retriever inside that same process. A
span opened through the OpenTelemetry *API* therefore joins the trace the
server already has open, so the keyword leg and the diversity pass appear
underneath the request that caused them, with no endpoint, exporter or service
name of our own.

Nothing here imports OpenViking. The global provider is the whole contract,
which keeps this independent of where OpenViking happens to keep its tracer.

With no provider installed -- tracing off in ``ov.conf``, or a unit test -- the
API returns non-recording spans: they enter a context manager, record nothing
and export nothing. That is why every call site can be left in place
unconditionally rather than guarded by a setting.

This module is a near-copy of ``ov_postgres.observability``. The two packages
are released separately and neither depends on the other at runtime, so a
shared module would mean one taking a dependency on the other for sixty lines.
The duplication is the cheaper of the two.

Why it earns its place here in particular: every addition this package makes
degrades to "do nothing" rather than to an error, and until now it said so only
in a debug log. A skipped keyword leg and a working one that found nothing look
identical from outside. The spans below record which happened, and why.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import ParamSpec, TypeVar, cast

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.util.types import AttributeValue

__all__ = ["annotate", "record_error", "traced"]

P = ParamSpec("P")
R = TypeVar("R")


@functools.lru_cache(maxsize=1)
def _tracer() -> trace.Tracer:
    """Return this package's tracer, resolved once.

    The version is read lazily rather than at import, so this module stays
    importable from anywhere in the package without caring what ``__init__``
    has finished doing.
    """
    from . import __version__

    return trace.get_tracer("ov_retrieval", __version__)


def traced(
    name: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Wrap an async method so each call opens a span called ``name``.

    Counts and outcomes are added from inside the method with :func:`annotate`,
    since they are not known when the span opens.

    An exception leaving the method is recorded on the span and sets its status
    to error; that is ``start_as_current_span``'s own default. Note that most
    of this package's failure paths never raise -- they are caught and degraded
    -- so the interesting outcomes are annotated, not recorded as exceptions.

    Parameters
    ----------
    name :
        Span name. Use the dotted method path, for example
        ``ov_retrieval.retrieve``.

    Returns
    -------
    Callable
        A decorator preserving the method's signature.

    Raises
    ------
    TypeError
        If applied to anything but a coroutine function. Every method traced
        here is async, and wrapping a sync one would hand its caller a
        coroutine nobody awaits. Raised at decoration time, so it surfaces on
        import rather than in production.
    """

    def decorate(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"{func.__qualname__} is sync; traced() wraps async methods")

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with _tracer().start_as_current_span(name):
                return await func(*args, **kwargs)

        # `self` rides in P rather than being pulled out with `Concatenate`:
        # that spelling makes the first parameter positional-only, and every
        # method here names it, which mypy rejects. The cast covers
        # `functools.wraps`, whose result is typed `_Wrapped`.
        return cast(Callable[P, Awaitable[R]], wrapper)

    return decorate


def annotate(attributes: Mapping[str, AttributeValue]) -> None:
    """Add attributes to the span currently open.

    Takes a mapping rather than keyword arguments because every name here is
    dotted -- ``ov_retrieval.pool`` is not a Python identifier.

    A no-op when nothing is recording, which is what makes it safe to call
    unconditionally: with no provider installed the current span is the API's
    invalid one, whose ``set_attribute`` does nothing.
    """
    span = trace.get_current_span()
    for key, value in attributes.items():
        span.set_attribute(key, value)


def record_error(exc: BaseException, outcome: str) -> None:
    """Record a swallowed exception on the current span and mark it failed.

    This package catches its own failures and returns a degraded ranking, so
    nothing propagates for the tracer to notice on its own. Without this a
    keyword leg that raised on every query would look, in a trace, exactly like
    one that ran and matched nothing.

    The status lands on the span for the operation that actually failed, not on
    the retrieval as a whole: the caller did get an answer, and marking the
    whole request failed would be a lie.

    Parameters
    ----------
    exc :
        The caught exception, recorded as a span event with its traceback.
    outcome :
        Short reason, used as the status description and recorded as
        ``ov_retrieval.outcome``.
    """
    span = trace.get_current_span()
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, outcome))
    span.set_attribute("ov_retrieval.outcome", outcome)
