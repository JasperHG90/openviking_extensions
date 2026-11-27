"""Run backend maintenance that needs the started server, beside it.

Some upkeep a vector backend owes OpenViking can only run once the server is
up. ov-postgres's md5 backfill is the case in point: after a schema reconcile
adds ``md5``, every old row has an empty one, and OpenViking's maintenance
reindex re-embeds every record whose md5 is empty. Filling it in means
hashing each stored file, and only the server's own ``VikingFS`` can read --
and decrypt -- those files.

So this wraps ``OpenVikingService.initialize``, as reflection's ticker does,
and starts one background task. It asks every adapter it can reach to run its
maintenance: the shared one, and each account's dedicated one, which
OpenViking creates the first time that account is used. It asks again every
``INTERVAL_SECONDS`` so a dedicated table reconciled later is covered too. An
adapter with nothing pending answers with one registry read.

An adapter takes part by having an ``async run_md5_backfill(viking_fs)``
method; anything else is left alone, so this costs nothing on other backends.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

__all__ = ["INTERVAL_SECONDS", "install", "run_once", "uninstall"]

logger = logging.getLogger(__name__)

_MODULE = "openviking.service.core"
_CLASS = "OpenVikingService"
_METHOD = "initialize"

# How often to ask the adapters again after the first pass.
INTERVAL_SECONDS = 300.0

_original: Any = None
_task: asyncio.Task[None] | None = None


def _adapters(vector_store: Any) -> Iterable[Any]:
    """Yield each distinct adapter the vector store holds right now."""
    seen: set[int] = set()
    shared = getattr(vector_store, "_shared_adapter", None)
    dedicated = [
        getattr(backend, "_adapter", None)
        for backend in list(getattr(vector_store, "_resolved_backends", {}).values())
    ]
    for adapter in [shared, *dedicated]:
        if adapter is not None and id(adapter) not in seen:
            seen.add(id(adapter))
            yield adapter


async def run_once(vector_store: Any, viking_fs: Any) -> int:
    """Run every reachable adapter's md5 backfill once.

    A failure in one adapter is logged and the others still run.

    Parameters
    ----------
    vector_store :
        The service's vector store.
    viking_fs :
        The service's ``VikingFS``.

    Returns
    -------
    int
        Rows filled across all adapters.
    """
    filled = 0
    for adapter in _adapters(vector_store):
        backfill = getattr(adapter, "run_md5_backfill", None)
        if backfill is None:
            continue
        try:
            filled += int(await backfill(viking_fs))
        except Exception:
            logger.exception("ov-ext maintenance: md5 backfill failed; retried later")
    return filled


async def _loop(service: Any) -> None:
    """Run the backfill now, then again every ``INTERVAL_SECONDS``."""
    while True:
        vector_store = getattr(service, "vikingdb_manager", None)
        viking_fs = getattr(service, "viking_fs", None)
        if vector_store is not None and viking_fs is not None:
            await run_once(vector_store, viking_fs)
        await asyncio.sleep(INTERVAL_SECONDS)


def install() -> None:
    """Start the maintenance task whenever OpenViking's service finishes starting.

    Idempotent.

    Raises
    ------
    RuntimeError
        If OpenViking no longer has the method this wraps.
    """
    global _original
    if _original is not None:
        return
    import importlib

    service_class = getattr(importlib.import_module(_MODULE), _CLASS, None)
    original = getattr(service_class, _METHOD, None)
    if service_class is None or original is None or not callable(original):
        raise RuntimeError(
            f"{_MODULE}.{_CLASS}.{_METHOD} is missing; OpenViking has moved it"
        )

    async def initialize_and_maintain(service: Any, *args: Any, **kwargs: Any) -> Any:
        """Run OpenViking's initialize, then start the maintenance task."""
        result = await original(service, *args, **kwargs)
        _start(service)
        return result

    _original = original
    setattr(service_class, _METHOD, initialize_and_maintain)


def _start(service: Any) -> None:
    """Launch the maintenance loop on the running event loop, once."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(service))
    _task.add_done_callback(_report_exit)


def _report_exit(task: asyncio.Task[None]) -> None:
    """Log a maintenance loop that ended on its own, which it should never do."""
    if not task.cancelled() and task.exception() is not None:
        logger.error("ov-ext maintenance: loop stopped", exc_info=task.exception())


def uninstall() -> None:
    """Cancel the task and put OpenViking's method back. Safe when not installed."""
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
