"""The periodic trigger: sweep, sleep, repeat, under a lock.

memex runs its reflection on a timer inside the server process, elected leader
by a Postgres advisory lock (``memex_core/scheduler.py``). This is the same
shape with the parts that only matter at scale left out -- no work queue, no
dead-letter table, no per-vault rotation, and one tick type rather than eight,
which is why there is a ``while`` loop here and not a scheduling library.

Three things a naive ``while True: sleep(n); work()`` gets wrong, all handled
below:

*It dies on the first exception.* One failed sweep would silently end
reflection for the life of the process, and nothing would say so. Every
iteration catches, logs and continues.

*It overlaps itself.* Sleeping first, or firing sweeps without awaiting them,
lets a sweep that outruns its interval start again while the first is still
running. The sleep happens after the sweep completes, so the interval is a gap
between runs rather than a period.

*It swallows cancellation.* ``CancelledError`` is an exception; catching it
broadly makes the task unkillable and hangs shutdown. It is re-raised.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import ReflectSettings
from .locks import SweepLock
from .ports import StructuredLLM
from .runner import run_sweep

__all__ = ["run_ticker"]

logger = logging.getLogger(__name__)


async def run_ticker(
    viking_fs: Any,
    vikingdb: Any,
    ctx: Any,
    lock: SweepLock,
    settings: ReflectSettings | None = None,
    *,
    llm: StructuredLLM | None = None,
) -> None:
    """Sweep on an interval until cancelled, one sweep at a time.

    Returns immediately when reflection is disabled, so a caller can start this
    unconditionally and let the setting decide.

    Parameters
    ----------
    viking_fs :
        OpenViking's filesystem.
    vikingdb :
        The vector store manager.
    ctx :
        Request context carrying the user and their permissions.
    lock :
        Held for the duration of each sweep. A tick that cannot take it skips,
        which is the normal state for every process that is not the leader.
    settings :
        Behaviour toggles. Read from the environment when omitted.
    llm :
        The model. Defaults to OpenViking's configured one.

    Raises
    ------
    asyncio.CancelledError
        On shutdown, re-raised rather than swallowed so the caller's
        ``task.cancel()`` actually ends it.
    """
    resolved = settings or ReflectSettings()
    if not resolved.enabled:
        logger.info("ov-ext reflect: disabled, no sweeps will run")
        return

    logger.info(
        "ov-ext reflect: sweeping every %ds, lock=%s, dry_run=%s",
        resolved.interval_seconds,
        resolved.lock,
        resolved.dry_run,
    )

    while True:
        try:
            # The lock goes to run_sweep rather than being taken here, so the
            # locking lives on the one path every sweep goes through -- there
            # is no second, unlocked way in.
            await run_sweep(viking_fs, vikingdb, ctx, resolved, lock=lock, llm=llm)
        except asyncio.CancelledError:
            logger.info("ov-ext reflect: ticker cancelled")
            raise
        except Exception:
            # One bad sweep must not end reflection for the life of the
            # process. Logged with a traceback, because the alternative is a
            # subsystem that stops working and never says why.
            logger.exception("ov-ext reflect: sweep failed; continuing")

        # After the sweep, not before: the interval is the gap between runs, so
        # a sweep slower than its interval cannot lap itself.
        await asyncio.sleep(resolved.interval_seconds)
