"""Build the delta store, when a deployment has configured one.

ov-ext does not depend on ov-postgres, and ov-postgres does not depend on
ov-ext. They agree on two shapes -- ``DeltaSink`` for the write and
``DeltaReader`` for the read -- and this is the one function that knows both
packages exist. The import is local and guarded, so a deployment on any other
backend never touches it.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import ENV_PREFIX, ReflectSettings

__all__ = ["build_delta_store"]

logger = logging.getLogger(__name__)

# Bounded because this pool exists only for deltas -- one insert per memory
# write and one read per sweep -- and every connection it holds is one the
# vector backend cannot.
_MIN_POOL = 1
_MAX_POOL = 4

# How long to wait for the startup probe. `ConnectionPool(open=True)` does not
# connect synchronously -- the failure surfaces at the first `connection()`,
# which otherwise waits out psycopg's 30-second default. This runs on the
# server's startup path, so a delta database that is down would add half a
# minute to every boot and every restart.
_PROBE_TIMEOUT_SECONDS = 3.0


def build_delta_store(settings: ReflectSettings) -> Any | None:
    """Return a delta store for ``settings``, or ``None``.

    ``None`` covers three cases a deployment should be able to sit in without
    anything failing: no DSN configured, ov-postgres not installed, or the
    database unreachable at startup. Reflection then reads whole memories, which
    is what it did before deltas existed.

    Parameters
    ----------
    settings :
        Behaviour toggles. ``deltas_dsn`` empty means "not configured".

    Returns
    -------
    Any | None
        Something satisfying both ``DeltaSink`` and ``DeltaReader``, or ``None``.
    """
    dsn = settings.deltas_dsn.strip()
    if not dsn:
        # Said out loud, because the half-configured case is silent and looks
        # like success: ov-postgres's `keep_deltas` creates the table, this
        # setting is what fills it, and they live in different packages. An
        # operator who set only the first sees a table that stays empty with
        # nothing anywhere explaining why.
        logger.info(
            "ov-ext reflect: %sDELTAS_DSN is not set, so no memory changes are "
            "captured and the sweep reads whole memories. If ov-postgres has "
            "`keep_deltas` on, its table will stay empty until this is set.",
            ENV_PREFIX,
        )
        return None
    try:
        from psycopg_pool import ConnectionPool

        # ov-postgres ships no py.typed marker, so mypy cannot see into it.
        from ov_postgres.deltas import (  # type: ignore[import-not-found]
            PgDeltaStore,
            delta_table_statements,
        )
    except ImportError:
        logger.error(
            "ov-ext reflect: OV_REFLECT_DELTAS_DSN is set but ov-postgres is not "
            "installed, so there is nowhere to read changes from; falling back "
            "to reading whole memories"
        )
        return None

    try:
        pool = ConnectionPool(
            dsn,
            min_size=_MIN_POOL,
            max_size=_MAX_POOL,
            timeout=_PROBE_TIMEOUT_SECONDS,
            open=True,
        )
        # The sweep may run against a database the vector backend bootstrapped
        # without `keep_deltas`, or against one it has never touched. Creating
        # the table here is idempotent and costs one statement at startup.
        with pool.connection() as conn, conn.cursor() as cur:
            for statement in delta_table_statements(settings.deltas_schema):
                cur.execute(statement)
            conn.commit()
    except Exception:
        logger.exception(
            "ov-ext reflect: could not open the delta store; falling back to "
            "reading whole memories"
        )
        return None

    logger.info(
        "ov-ext reflect: reading changes from %s in the delta store",
        settings.deltas_schema,
    )
    return PgDeltaStore(pool, settings.deltas_schema)
