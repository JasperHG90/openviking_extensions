"""Start the sweep ticker when OpenViking's service finishes booting.

The ticker needs four things that do not exist when :func:`ov_ext.install`
runs: an event loop, a ``VikingFS``, a ``VikingDBManager``, and a request
context. All four appear during ``OpenVikingService.initialize``, which is also
where OpenViking starts its own background work -- the session auto-commit
scheduler and the watch scheduler. So the ticker starts in the same place, by
wrapping that method.

This is a monkeypatch on somebody else's package, like the retrieval one, and
it takes the same precautions: :func:`install` verifies what it is wrapping and
refuses to guess, so an upstream rename surfaces as a refused startup rather
than as reflection quietly never running.

Unlike the retrieval patch it rebinds a *method*, not a module attribute, so
the wrapper delegates to the original and is idempotent -- installing twice
does not stack two tickers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import ReflectSettings
from .locks import build_lock
from .ticker import run_ticker

__all__ = ["install", "uninstall"]

logger = logging.getLogger(__name__)

_MODULE = "openviking.service.core"
_CLASS = "OpenVikingService"
_METHOD = "initialize"

# The method displaced by install(), so uninstall() can put it back. Module
# state because the patch is process-global, as is the task it starts.
_original: Any = None
_task: asyncio.Task[None] | None = None


def install(settings: ReflectSettings | None = None) -> None:
    """Arrange for the sweep ticker to start with OpenViking's service.

    Does nothing when reflection is disabled, which is the default.

    Parameters
    ----------
    settings :
        Behaviour toggles. Read from the environment when omitted.

    Raises
    ------
    RuntimeError
        If the method to wrap is not where it should be. Better a refused
        startup than a subsystem that silently never runs.
    ValueError
        If no sweep lock has been chosen. Raised here, at startup, rather than
        at the first tick -- see :func:`ov_ext.reflect.locks.build_lock`.
    """
    global _original
    resolved = settings or ReflectSettings()
    if not resolved.enabled:
        logger.debug("ov-ext reflect: disabled, ticker not installed")
        return

    # Fail now if the lock is unset, while the operator is watching a startup
    # log, rather than in fifteen minutes inside a background task.
    build_lock(resolved)

    if _original is not None:
        return

    import importlib

    module = importlib.import_module(_MODULE)
    service_class = getattr(module, _CLASS, None)
    if service_class is None:
        raise RuntimeError(f"{_MODULE} has no {_CLASS}; OpenViking has moved it")
    original = getattr(service_class, _METHOD, None)
    if original is None or not callable(original):
        raise RuntimeError(f"{_CLASS} has no callable {_METHOD}; OpenViking has moved it")

    async def initialize_and_start(service: Any, *args: Any, **kwargs: Any) -> Any:
        """Run OpenViking's initialize, then start the ticker beside it."""
        result = await original(service, *args, **kwargs)
        _start_ticker(service, resolved)
        return result

    _original = original
    setattr(service_class, _METHOD, initialize_and_start)
    logger.info("ov-ext reflect: ticker will start with the service")


def uninstall() -> None:
    """Cancel the ticker and put OpenViking's method back.

    Safe to call when nothing was installed.
    """
    global _original, _task
    if _task is not None:
        _task.cancel()
        _task = None
    if _original is None:
        return

    import importlib

    service_class = getattr(importlib.import_module(_MODULE), _CLASS)
    setattr(service_class, _METHOD, _original)
    _original = None


def _start_ticker(service: Any, settings: ReflectSettings) -> None:
    """Launch the ticker as a background task on the running loop.

    Failures here are logged rather than raised: the caller is OpenViking's
    startup path, and reflection is an extension. A server that cannot reflect
    should still serve.
    """
    global _task
    if _task is not None and not _task.done():
        return

    viking_fs = getattr(service, "viking_fs", None)
    vikingdb = getattr(service, "vikingdb_manager", None)
    if viking_fs is None or vikingdb is None:
        logger.error(
            "ov-ext reflect: service exposes no viking_fs/vikingdb_manager after "
            "initialize; ticker not started"
        )
        return

    ctx = _root_context()
    if ctx is None:
        return

    _task = asyncio.create_task(
        run_ticker(viking_fs, vikingdb, ctx, build_lock(settings), settings)
    )
    # Without a reference the loop may garbage-collect the task mid-sweep;
    # `_task` is that reference, and it is also what uninstall() cancels.
    _task.add_done_callback(_log_if_it_died)


def _root_context() -> Any:
    """Build the request context sweeps run under, or ``None`` if it cannot be.

    Reflection is not serving a request, so it has no context to inherit and
    has to construct one. It reads and writes one user's memories, named by
    ``OV_REFLECT_USER_ID``.
    """
    from .config import ReflectSettings

    settings = ReflectSettings()
    if not settings.user_id.strip():
        logger.error(
            "ov-ext reflect: OV_REFLECT_USER_ID is not set, so there is no user "
            "whose memories to reflect on; ticker not started"
        )
        return None
    try:
        from openviking.server.identity import RequestContext, Role, UserIdentifier
    except ImportError:  # pragma: no cover - depends on the OpenViking version
        logger.exception("ov-ext reflect: cannot build a request context")
        return None
    return RequestContext(
        user=UserIdentifier(settings.account_id, settings.user_id), role=Role.USER
    )


def _log_if_it_died(task: asyncio.Task[None]) -> None:
    """Report a ticker that ended on its own, which it should never do."""
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.error("ov-ext reflect: ticker stopped", exc_info=exception)
