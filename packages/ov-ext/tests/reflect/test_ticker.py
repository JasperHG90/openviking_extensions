"""The periodic trigger: that it sweeps, holds the lock, and can be stopped.

The three things a naive loop gets wrong each have a test here, because each
one fails silently: a ticker that died on an exception, one that overlapped
itself, and one that could not be cancelled all look like a ticker that is
working until you go looking.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

import pytest

from ov_ext.reflect.config import LockKind, ReflectSettings
from ov_ext.reflect.locks import ProcessLock
from ov_ext.reflect.ticker import run_ticker

pytestmark = pytest.mark.usefixtures("clean_env")


class NeverLock:
    """A lock nobody can take, standing in for another process holding it."""

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        yield False


class CountingLock:
    """A lock that records how many times it was taken."""

    def __init__(self) -> None:
        self.taken = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        self.taken += 1
        yield True


def fast(**overrides: Any) -> ReflectSettings:
    """Settings whose interval is short enough to see several ticks in a test.

    A real sleep with a tiny interval, rather than a patched `asyncio.sleep`:
    patching it reaches every coroutine in the interpreter, including the
    helper doing the patching.
    """
    return settings(interval_seconds=0.01, **overrides)


def settings(**overrides: Any) -> ReflectSettings:
    """Settings with reflection on, a lock chosen, and no sleeping."""
    base = ReflectSettings().model_dump()
    base.update(
        {
            "enabled": True,
            "user_id": "jasper",
            "lock": LockKind.PROCESS,
            "interval_seconds": 1,
        }
    )
    base.update(overrides)
    return ReflectSettings.model_construct(**base)


async def run_briefly(coro: Any, *, ticks: float = 0.05) -> None:
    """Run a ticker, then cancel it the way a shutdown would."""
    task = asyncio.create_task(coro)
    await asyncio.sleep(ticks)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_disabled_ticker_returns_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a caller can start it unconditionally and let the setting decide."""
    calls: list[int] = []
    monkeypatch.setattr(
        "ov_ext.reflect.ticker.run_sweep",
        lambda *a, **k: calls.append(1),
    )
    # Returns rather than hanging: no cancellation needed.
    await asyncio.wait_for(
        run_ticker(None, None, None, ProcessLock(), settings(enabled=False)),
        timeout=1,
    )
    assert calls == []


async def test_it_sweeps_while_it_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swept = asyncio.Event()

    async def sweep(*args: Any, **kwargs: Any) -> None:
        swept.set()

    monkeypatch.setattr("ov_ext.reflect.ticker.run_sweep", sweep)
    lock = CountingLock()
    task = asyncio.create_task(run_ticker(None, None, None, lock, settings()))
    await asyncio.wait_for(swept.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.taken >= 1


async def test_it_does_not_sweep_when_another_process_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a follower ticks, finds the lock taken, and does nothing."""
    calls: list[int] = []

    async def sweep(*args: Any, **kwargs: Any) -> None:
        calls.append(1)

    monkeypatch.setattr("ov_ext.reflect.ticker.run_sweep", sweep)
    await run_briefly(run_ticker(None, None, None, NeverLock(), settings()))
    assert calls == []


async def test_a_failing_sweep_does_not_end_the_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise one bad sweep silently ends reflection for the process lifetime."""
    attempts: list[int] = []
    third = asyncio.Event()

    async def sweep(*args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        if len(attempts) >= 3:
            third.set()
        raise RuntimeError("upstream down")

    monkeypatch.setattr("ov_ext.reflect.ticker.run_sweep", sweep)
    task = asyncio.create_task(
        run_ticker(None, None, None, ProcessLock(), fast())
    )
    await asyncio.wait_for(third.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(attempts) >= 3


async def test_it_stops_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catching CancelledError broadly would make the task unkillable."""
    monkeypatch.setattr("ov_ext.reflect.ticker.run_sweep", _no_sweep)
    task = asyncio.create_task(
        run_ticker(None, None, None, ProcessLock(), settings())
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_cancelling_mid_sweep_still_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must not wait for a sweep that is stuck on a model call."""
    in_sweep = asyncio.Event()

    async def sweep(*args: Any, **kwargs: Any) -> None:
        in_sweep.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("ov_ext.reflect.ticker.run_sweep", sweep)
    task = asyncio.create_task(
        run_ticker(None, None, None, ProcessLock(), settings())
    )
    await asyncio.wait_for(in_sweep.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


async def test_a_slow_sweep_never_overlaps_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interval is a gap between runs, not a period to fire on.

    Sleeping first, or firing without awaiting, would let a sweep slower than
    its interval start again while the first was still running.
    """
    concurrent = 0
    peak = 0
    done = asyncio.Event()
    runs = 0

    async def sweep(*args: Any, **kwargs: Any) -> None:
        nonlocal concurrent, peak, runs
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1
        runs += 1
        if runs >= 3:
            done.set()

    monkeypatch.setattr("ov_ext.reflect.ticker.run_sweep", sweep)
    task = asyncio.create_task(
        run_ticker(None, None, None, ProcessLock(), fast())
    )
    await asyncio.wait_for(done.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert peak == 1


async def _no_sweep(*args: Any, **kwargs: Any) -> None:
    """A sweep that does nothing."""
