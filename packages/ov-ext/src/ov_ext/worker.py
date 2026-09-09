"""Carry the patches into uvicorn's worker processes.

``ov-ext-server`` installs into the process it runs in. That is the whole story
while ``workers = 1``, because the same process then serves every request. Past
one, it is not:

``openviking/server/bootstrap.py:327`` hands uvicorn an *import string* --
``"openviking.server.app:create_worker_app"`` -- and uvicorn spawns children
with ``multiprocessing.get_context("spawn")`` (``uvicorn/_subprocess.py:18``).
A spawned child is a fresh interpreter with an empty ``sys.modules``. It imports
that one string and nothing else, so it never runs ``ov_ext.__main__:main``,
never calls :func:`ov_ext.install`, and never sees a patch. Measured: the parent
reports ``HybridRetriever`` while the child reports ``HierarchicalRetriever``
and has not imported ``ov_ext`` at all.

A fork would have inherited the patched modules and none of this would matter.
uvicorn does not fork.

So the parent, which does run our code, rewrites the string it passes uvicorn to
name :func:`create_worker_app` here instead. Each child then imports *this*
module, installs, and delegates to OpenViking's factory -- so a worker serves
requests through the same patched retriever the single-process case does.

Settings reach the children through the environment, because that is all a
spawned process inherits. A deployment running more than one worker has to
configure ov-ext with ``OV_*`` variables rather than by passing objects to
:func:`ov_ext.install`; arguments live in the parent's memory and do not
survive the spawn.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["create_worker_app", "install", "uninstall"]

logger = logging.getLogger(__name__)

# What OpenViking hands uvicorn in multi-worker mode. Checked rather than
# assumed: if upstream renames its factory, the parent should say so instead of
# silently rewriting a string that no longer means what we think.
OPENVIKING_FACTORY = "openviking.server.app:create_worker_app"
OUR_FACTORY = "ov_ext.worker:create_worker_app"

_original: Any = None


def create_worker_app() -> Any:
    """Install ov-ext, then build OpenViking's worker app.

    Runs inside a spawned worker, where nothing else of ours has run. Settings
    come from the environment for the reason in this module's docstring.

    Returns
    -------
    Any
        The ``FastAPI`` application OpenViking's own factory builds.
    """
    from .installer import install

    install()
    from openviking.server.app import create_worker_app as openviking_factory

    logger.info("ov-ext: installed in worker process")
    return openviking_factory()


def install() -> None:
    """Make uvicorn's workers import :func:`create_worker_app` instead.

    Wraps ``uvicorn.run``. The parent is the only process that runs our code,
    and the import string is the only thing it can hand a child that survives a
    spawn, so this is where the two meet.

    Calling twice is a no-op rather than a second wrapper.
    """
    global _original
    if _original is not None:
        return

    import uvicorn

    original = uvicorn.run

    def run(app: Any = None, **kwargs: Any) -> Any:
        """Redirect a multi-worker launch through ov-ext's factory."""
        workers = kwargs.get("workers") or 1
        if workers > 1 and isinstance(app, str):
            _check_multiworker_is_configured_for(workers)
            if app == OPENVIKING_FACTORY:
                logger.info(
                    "ov-ext: %d workers; routing them through %s", workers, OUR_FACTORY
                )
                app = OUR_FACTORY
            elif app != OUR_FACTORY:
                # Not the string we know how to wrap. Rewriting it would be a
                # guess, and a wrong guess here breaks the whole server.
                logger.error(
                    "ov-ext: cannot reach %d workers -- uvicorn was given %r, not "
                    "%r. Retrieval and reflection will NOT be active in the "
                    "workers. Run with workers=1, or report this: OpenViking has "
                    "changed how it launches them.",
                    workers,
                    app,
                    OPENVIKING_FACTORY,
                )
        return original(app, **kwargs)

    _original = original
    uvicorn.run = run


def uninstall() -> None:
    """Put ``uvicorn.run`` back. Safe when nothing was installed."""
    global _original
    if _original is None:
        return
    import uvicorn

    uvicorn.run = _original
    _original = None


def _check_multiworker_is_configured_for(workers: int) -> None:
    """Refuse a sweep lock that cannot be correct across ``workers`` processes.

    ``OV_REFLECT_LOCK=process`` is an assertion that exactly one process
    sweeps. Once uvicorn is about to start several, each with a ticker of its
    own, that assertion is false -- and a process-local lock cannot detect the
    others, so the failure would be silent duplicate sweeps rather than an
    error.

    Raises
    ------
    ValueError
        When reflection is enabled with a process-local lock and more than one
        worker.
    """
    from .reflect.config import ENV_PREFIX, LockKind, ReflectSettings

    settings = ReflectSettings()
    if not settings.enabled or settings.lock is not LockKind.PROCESS:
        return
    raise ValueError(
        f"{ENV_PREFIX}LOCK=process asserts that exactly one process sweeps, but "
        f"the server is starting {workers} workers and each runs its own "
        f"ticker. Set {ENV_PREFIX}LOCK=postgres (with {ENV_PREFIX}LOCK_DSN), or "
        f"run a single worker."
    )
