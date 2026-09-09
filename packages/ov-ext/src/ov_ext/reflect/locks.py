"""Mutual exclusion for the sweep, so two of them never run at once.

Two sweeps running together is not a performance problem, it is a correctness
one. Both read the same watermark, both gather the same memories, both propose,
and both advance the mark -- so each silently consumes work the other was
half-way through, and the store gets two observations drawn from one batch.

The lock is therefore not optional. :mod:`ov_ext.reflect.ticker` refuses to
start without one, and the choice is a setting with no default, because the
safe answer depends on a fact only the operator knows: how many processes run.

Two implementations.

``ProcessLock``
    An ``asyncio.Lock``. Correct for exactly one process, and correct there by
    construction -- one event loop, one holder. Choosing it is an assertion
    that you run one process, which is why the setting spells it out rather
    than defaulting to it.

``PostgresAdvisoryLock``
    ``pg_try_advisory_lock`` on a dedicated connection, for any deployment with
    more than one. This is what memex's scheduler does.

Why Postgres and not Redis
--------------------------

A Redis ``SET NX PX`` lock expires on a timer. A holder that stalls past its
TTL -- a GC pause, an IO stall, a frozen VM -- loses the lock while still
believing it holds it, and a second sweeper starts. Closing that requires
fencing tokens *validated at the resource*, and OpenViking's ``write_file``
validates nothing, so there is no token to fence with. Redlock does not fix
this.

A Postgres advisory lock is session-scoped: held until released or until the
connection ends. A crashed process, a killed container or a severed network
drops the connection, and the lock goes with it. No TTL to tune, no clock to
trust, no renewal to miss.

Session-scoped is not the same as sweep-scoped
---------------------------------------------

The lock lives on a connection, and that connection is *idle* for the whole
sweep -- everything the sweep does goes through OpenViking, not through here.
An idle connection is exactly what a database reaps. Postgres 14's
``idle_session_timeout``, PgBouncer's ``server_idle_timeout``, a failover, or an
admin running ``pg_terminate_backend`` all drop it, and the lock goes with it
**while the sweep is still running** -- which is the same double-sweep the TTL
argument above rejects Redis for. Measured against a real server with
``idle_session_timeout = 1500ms`` and a four-second sweep: a second caller took
the lock mid-sweep.

So the lock does two more things:

*It refuses to be reaped.* ``idle_session_timeout`` is set to 0 for the lock
session, so the server's own reaper leaves it alone however long the sweep runs.

*It checks it still holds what it thinks it holds.* A heartbeat pings the
connection every :data:`HEARTBEAT_SECONDS`; a ping that fails means the lock is
gone, and the sweep holding it is cancelled rather than left to finish
unprotected. The exposure is therefore bounded by the heartbeat interval rather
than by the length of a sweep.

Two constraints follow, and they are load-bearing:

* the DSN must be a **direct** connection, not a transaction-pooled proxy --
  PgBouncer in transaction mode hands a different backend to each statement, so
  a session lock taken on one is invisible to the next;
* every sweeper must point at the **same database**. Advisory locks are scoped
  per database, so two DSNs differing only in database name both grant the lock.

The remaining cost is liveness: a holder that is alive, connected and wedged
keeps the lock, and nothing sweeps until it is killed. That is the right trade
for a periodic job -- a sweep that does not happen this hour is recoverable, one
that happens twice corrupts the watermark.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

__all__ = [
    "LOCK_NAMESPACE",
    "PostgresAdvisoryLock",
    "ProcessLock",
    "SweepLock",
    "advisory_key",
    "build_lock",
]

logger = logging.getLogger(__name__)

# Namespaced so the key cannot collide with another advisory lock in the same
# database -- ov-postgres takes one during schema bootstrap, and OpenViking may
# take others.
LOCK_NAMESPACE = "ov_ext.reflect.sweep"

# How often the holder checks its lock connection is still alive. Short enough
# that a reaped or severed connection is noticed long before a sweep ends, long
# enough that it costs nothing. Also the upper bound on how long a second
# sweeper could overlap the first if the connection dies.
HEARTBEAT_SECONDS = 5.0


def advisory_key(namespace: str = LOCK_NAMESPACE) -> int:
    """Derive a stable signed 64-bit key for ``pg_try_advisory_lock``.

    Hashed rather than hand-picked so two deployments sharing a database but
    reflecting different stores can namespace themselves apart, and folded into
    the signed 64-bit range because that is what the Postgres function takes.

    Parameters
    ----------
    namespace :
        String identifying what is being locked.

    Returns
    -------
    int
        A value in ``[-2**63, 2**63)``, stable across processes and releases.
    """
    digest = hashlib.sha256(namespace.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


class SweepLock(Protocol):
    """Something that lets at most one sweep run at a time."""

    @asynccontextmanager
    def acquire(self) -> AsyncIterator[bool]:
        """Yield whether the lock was taken, releasing it on exit.

        Never blocks and never raises on contention: a tick that cannot get the
        lock is a follower doing the normal thing, not an error. It yields
        ``False`` and the caller skips this round.
        """
        ...  # pragma: no cover - protocol body


class ProcessLock:
    """Mutual exclusion within one process, and no further.

    Correct only when exactly one process runs sweeps. Nothing here can detect
    a second one, which is why :mod:`ov_ext.reflect.config` makes choosing this
    an explicit statement rather than a default someone inherits.

    Also guards against a sweep overrunning its own interval: the ticker awaits
    each sweep before sleeping again, so this is belt and braces, but a future
    caller that fires sweeps concurrently gets the same protection.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        """Yield True if this task got the lock, False if another holds it."""
        if self._lock.locked():
            yield False
            return
        async with self._lock:
            yield True


class PostgresAdvisoryLock:
    """Mutual exclusion across every process reaching the same database.

    Holds ``pg_try_advisory_lock`` on a connection opened for the attempt and
    closed on release, so the lock's lifetime is exactly the sweep's and a
    process that dies mid-sweep releases it by dropping the connection.

    A connection per attempt rather than one held for the ticker's lifetime:
    that way a follower takes over on its next tick after a leader dies, with
    no failover logic and no lease to reason about. The cost is one connect per
    tick, which against an interval measured in minutes is nothing.

    Parameters
    ----------
    dsn :
        libpq connection string for the database to lock in. Every process that
        sweeps must name the **same database**: advisory locks are scoped per
        database, so two DSNs differing only in database name both grant the
        lock and both sweep. It must also be a direct connection -- PgBouncer
        in transaction mode gives each statement a different backend, which
        makes a session lock meaningless. The natural choice is the database
        already backing the store.
    key :
        Advisory lock key. Defaults to a hash of :data:`LOCK_NAMESPACE`.
    heartbeat_seconds :
        How often to check the lock connection is still alive, and the upper
        bound on how long a second sweeper could overlap the first if it is
        not.
    """

    def __init__(
        self,
        dsn: str,
        key: int | None = None,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._dsn = dsn
        self._key = advisory_key() if key is None else key
        self._heartbeat = heartbeat_seconds

    @staticmethod
    def _connect(dsn: str) -> Any:
        """Open a psycopg connection, explaining the dependency if it is absent.

        Imported here rather than at module scope so psycopg stays out of this
        package's runtime dependencies: it is needed only by deployments that
        choose this lock.
        """
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "OV_REFLECT_LOCK=postgres needs psycopg, which is not installed. "
                "Install it, or set OV_REFLECT_LOCK=process if exactly one "
                "process runs sweeps."
            ) from exc
        conn = psycopg.connect(dsn, autocommit=True)
        # The lock connection is idle for the whole sweep, which is precisely
        # what `idle_session_timeout` exists to kill. Disabling it for this
        # session stops the server dropping the lock out from under a sweep
        # that is still running. Scoped to this connection, so it changes
        # nothing for anyone else.
        with conn.cursor() as cur:
            cur.execute("SET idle_session_timeout = 0")
        return conn

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        """Yield True if this process took the lock, False if another holds it.

        The connection is closed on the way out whether or not the lock was
        taken, and whether or not the body raised. Closing is what releases the
        lock, so an explicit unlock would be redundant -- and would be the only
        path that could leak it if the body raised.
        """
        # psycopg's sync API blocks, so it goes to a thread. The alternative,
        # psycopg_pool's async connection, would add a second dependency for
        # one connect per interval.
        conn = await asyncio.to_thread(self._connect, self._dsn)
        heartbeat: asyncio.Task[None] | None = None
        try:
            got = await asyncio.to_thread(self._try_lock, conn, self._key)
            if not got:
                logger.debug(
                    "ov-ext reflect: another process holds the sweep lock; skipping"
                )
            else:
                # The task that will run the sweep is this one, because
                # `acquire` is used as `async with ...: await run_sweep(...)`.
                # Capturing it here is what lets the heartbeat stop a sweep
                # that has lost its lock.
                heartbeat = asyncio.create_task(
                    self._heartbeat_until_lost(conn, asyncio.current_task())
                )
            yield got
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            await asyncio.to_thread(conn.close)

    @staticmethod
    def _try_lock(conn: Any, key: int) -> bool:
        """Take the advisory lock without waiting, returning whether it was taken."""
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            row = cur.fetchone()
        return bool(row and row[0])

    async def _heartbeat_until_lost(
        self, conn: Any, holder: asyncio.Task[Any] | None
    ) -> None:
        """Ping the lock connection, cancelling ``holder`` when it stops answering.

        Two jobs in one query. The ping keeps the session from looking idle,
        which is what a reaper acts on; and a ping that raises means the
        connection -- and therefore the lock -- is gone, so whatever is
        sweeping under it must stop rather than run on unprotected.

        psycopg does not reconnect transparently, so a failed statement really
        does mean the session is dead rather than merely interrupted.
        """
        while True:
            await asyncio.sleep(self._heartbeat)
            try:
                await asyncio.to_thread(self._ping, conn)
            except Exception as exc:
                logger.error(
                    "ov-ext reflect: lost the sweep lock mid-sweep (%s); "
                    "cancelling the sweep so a second one cannot overlap it",
                    exc,
                )
                if holder is not None:
                    holder.cancel()
                return

    @staticmethod
    def _ping(conn: Any) -> None:
        """Run a trivial statement, raising if the session is gone."""
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()


def build_lock(settings: Any) -> SweepLock:
    """Return the lock a deployment's settings ask for.

    Parameters
    ----------
    settings :
        A :class:`ov_ext.reflect.config.ReflectSettings`.

    Returns
    -------
    SweepLock
        A process-local lock or a Postgres advisory lock.

    Raises
    ------
    ValueError
        When no lock has been chosen. This is the guarantee's last mile: there
        is deliberately no fallback, because every fallback is a way to end up
        sweeping unlocked by forgetting a setting rather than by deciding to.
    """
    from .config import ENV_PREFIX, LockKind

    if settings.lock is LockKind.POSTGRES:
        return PostgresAdvisoryLock(settings.lock_dsn)
    if settings.lock is LockKind.PROCESS:
        return ProcessLock()
    raise ValueError(
        f"{ENV_PREFIX}LOCK is not set. Two sweeps running at once corrupt the "
        f"watermark, so reflection will not start without one. Set "
        f"{ENV_PREFIX}LOCK=postgres (with {ENV_PREFIX}LOCK_DSN) when more than "
        f"one process may sweep, or {ENV_PREFIX}LOCK=process to assert that "
        f"exactly one does."
    )
