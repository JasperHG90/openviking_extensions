"""OpenTelemetry spans for the pgvector backend.

There is nothing to configure here, which is the point. OpenViking's server
installs a process-global ``TracerProvider`` at startup -- ``app.py`` calls
``init_tracer_from_server_config`` from ``server.observability.traces`` -- and
this package runs inside that process. A span opened through the OpenTelemetry
*API* therefore joins the trace the server already has open, so a pgvector
query appears as a child of the request that caused it, with no endpoint,
exporter or service name of our own.

Nothing here imports OpenViking. The global provider is the whole contract,
which keeps this independent of where OpenViking happens to keep its tracer,
and leaves the spans just as usable under ``opentelemetry-instrument``.

With no provider installed -- tracing off in ``ov.conf``, or a unit test -- the
API returns non-recording spans: they enter a context manager, record nothing
and export nothing. That is why every call site can be left in place
unconditionally rather than guarded by a setting.

Ordering does not matter either. ``trace.get_tracer`` hands back a
``ProxyTracer`` that resolves the provider on first use, so a tracer taken
before the server configured tracing still records once it has.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from typing import ParamSpec, TypeVar, cast

from opentelemetry import trace
from opentelemetry.util.types import AttributeValue

__all__ = ["DB_SYSTEM", "annotate", "traced"]

# Database attributes use OpenTelemetry's own semantic conventions, so these
# spans group with everything else talking to PostgreSQL in a backend that
# knows the names. Attributes this package invents carry an `ov_postgres.`
# prefix instead, where they cannot collide with a convention that later
# defines the same word.
DB_SYSTEM = "postgresql"

P = ParamSpec("P")
R = TypeVar("R")


@functools.lru_cache(maxsize=1)
def _tracer() -> trace.Tracer:
    """Return this package's tracer, resolved once.

    The version is read lazily rather than at import: ``__init__`` imports the
    adapter, which imports this module, so a top-level ``from . import
    __version__`` would run before the package had set it.
    """
    from . import __version__

    return trace.get_tracer("ov_postgres", __version__)


def traced(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a method so each call opens a span called ``name``.

    The span carries the instance's ``_span_attributes`` when it has any, which
    is how every collection span gets its schema and table without repeating
    them at fifteen call sites. Results and counts are added from inside the
    method with :func:`annotate`, since they are not known when the span opens.

    An exception leaving the method is recorded on the span and sets its status
    to error; that is ``start_as_current_span``'s own default, not something
    added here.

    Parameters
    ----------
    name :
        Span name. Use the dotted method path, for example
        ``ov_postgres.search_by_vector``.

    Returns
    -------
    Callable
        A decorator preserving the method's signature.

    Raises
    ------
    TypeError
        If applied to a coroutine function. Nothing in this package is async,
        and the wrapper below would turn one into a coroutine nobody awaits.
        Raised at decoration time, so it surfaces on import rather than in
        production.
    """

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(func):
            raise TypeError(f"{func.__qualname__} is async; traced() wraps sync methods")

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # `self` is args[0] for the methods this decorates. Read that way
            # rather than pulled out with `Concatenate`, which would make the
            # first parameter positional-only -- and every method here names
            # it, which mypy rejects.
            instance = args[0] if args else None
            attributes: Mapping[str, AttributeValue] | None = getattr(
                instance, "_span_attributes", None
            )
            with _tracer().start_as_current_span(name, attributes=attributes):
                return func(*args, **kwargs)

        # The cast covers `functools.wraps`, whose result is typed `_Wrapped`.
        return cast(Callable[P, R], wrapper)

    return decorate


def annotate(attributes: Mapping[str, AttributeValue]) -> None:
    """Add attributes to the span currently open.

    Takes a mapping rather than keyword arguments because every name here is
    dotted -- ``ov_postgres.rows`` is not a Python identifier.

    A no-op when nothing is recording, which is what makes it safe to call
    unconditionally: with no provider installed the current span is the API's
    invalid one, whose ``set_attribute`` does nothing.
    """
    span = trace.get_current_span()
    for key, value in attributes.items():
        span.set_attribute(key, value)
