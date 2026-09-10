"""Where memory changes are kept, so reflection can read the change.

An OpenViking memory is one file that accretes for months. Reflection wants the
few lines that changed, not the twenty kilobytes around them, and there is
nowhere to read that from after the fact: the store keeps no version history
unless someone calls the snapshot API, and the index holds one row per file. So
the change is recorded as it is applied, and this is where it lands.

Postgres rather than the object store on purpose. A busy day produces hundreds
of small records -- one per edited passage -- and object stores charge per
request and hide no index. This is a narrow append-only table next to the vectors
it describes, and reading "what changed since the watermark" is one indexed
range scan rather than a listing.

The table lives in the same schema as the collections, and is created by the
same bootstrap, but nothing else here reads it. It is written by ov-ext's
capture hook and drained by its reflect sweep; the vector backend neither
consults it nor joins against it.

Nothing in this module is reached unless ``keep_deltas`` is on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from psycopg import sql
from psycopg_pool import ConnectionPool

__all__ = ["DELTA_TABLE", "DeltaRecord", "PgDeltaStore", "delta_table_statements"]

logger = logging.getLogger(__name__)

DELTA_TABLE = "ov_memory_deltas"


class DeltaLike(Protocol):
    """One recorded change, as ov-ext's capture describes it.

    Structural rather than imported: ov-postgres does not depend on ov-ext, and
    ov-ext does not depend on ov-postgres. Both packages agree on this shape and
    neither imports the other.

    Declared as read-only properties rather than plain attributes. A bare
    annotation in a Protocol means a *settable* member, which a frozen
    dataclass -- exactly what ov-ext passes -- cannot satisfy, so the one
    hand-off this module exists for would fail the type gate.
    """

    @property
    def uri(self) -> str:
        """The memory that changed."""

    @property
    def memory_type(self) -> str:
        """Its OpenViking memory type, for example ``entities``."""

    @property
    def field(self) -> str:
        """The memory field the change lands in, usually ``content``."""

    @property
    def search(self) -> str:
        """The text that was there. Empty when nothing was replaced."""

    @property
    def replace(self) -> str:
        """The text that replaced it. Empty when the change was a deletion."""

    @property
    def created(self) -> bool:
        """Whether the memory did not exist before this write."""

    @property
    def changed_at(self) -> datetime:
        """When the change happened, in UTC."""


class DeltaRecord(dict[str, Any]):
    """One delta as it comes back out of the table.

    A dict subclass rather than a model: it crosses no boundary that needs
    validating -- it was written by this module and read by this module -- and
    the caller reshapes it into whatever it prompts with.
    """


def delta_table_statements(schema_name: str) -> list[sql.SQL | sql.Composed]:
    """Build the statements creating the delta table and its index.

    Idempotent, and safe to run beside a table that already holds rows. Called
    from the bootstrap so a backend with ``keep_deltas`` on has somewhere to
    write before the first memory is edited.

    Parameters
    ----------
    schema_name :
        PostgreSQL schema the collections live in.

    Returns
    -------
    list[sql.SQL | sql.Composed]
        Statements to execute in order, inside the bootstrap transaction.
        Spelled as the union rather than ``Composable`` to match ``ddl``'s
        ``Statement`` alias, which cannot be imported here without a cycle.
    """
    ns = sql.Identifier(schema_name)
    table = sql.Identifier(DELTA_TABLE)
    return [
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {}.{} (
                id           bigserial PRIMARY KEY,
                uri          text        NOT NULL,
                memory_type  text        NOT NULL,
                field        text        NOT NULL,
                search       text        NOT NULL,
                replace      text        NOT NULL,
                created      boolean     NOT NULL,
                changed_at   timestamptz NOT NULL,
                reflected_at timestamptz
            )
            """
        ).format(ns, table),
        # The sweep's only query: unreflected rows, oldest first. Partial, so
        # the index holds only what is still pending and shrinks as the sweep
        # drains it rather than growing with history.
        sql.SQL(
            "CREATE INDEX IF NOT EXISTS {} ON {}.{} (changed_at) "
            "WHERE reflected_at IS NULL"
        ).format(
            sql.Identifier(f"{DELTA_TABLE}_pending_idx"),
            ns,
            table,
        ),
        # Merges rewrite by URI, and so does the per-memory read.
        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (uri)").format(
            sql.Identifier(f"{DELTA_TABLE}_uri_idx"),
            ns,
            table,
        ),
    ]


class PgDeltaStore:
    """Reads and writes memory deltas in PostgreSQL.

    Satisfies the ``DeltaSink`` protocol ov-ext's capture hook expects, without
    either package importing the other.

    Parameters
    ----------
    pool :
        Connection pool, shared with the vector backend so deltas cost no extra
        connections.
    schema_name :
        Schema holding the delta table.
    """

    def __init__(self, pool: ConnectionPool, schema_name: str) -> None:
        self._pool = pool
        self._schema = schema_name

    def _table(self) -> sql.Composed:
        """Return the qualified delta table name."""
        return sql.SQL("{}.{}").format(
            sql.Identifier(self._schema), sql.Identifier(DELTA_TABLE)
        )

    def record_sync(self, deltas: Sequence[DeltaLike]) -> None:
        """Append a batch of deltas, blocking.

        Parameters
        ----------
        deltas :
            What changed. An empty batch is a no-op and touches no connection.
        """
        if not deltas:
            return
        rows = [
            (
                delta.uri,
                delta.memory_type,
                delta.field,
                delta.search,
                delta.replace,
                delta.created,
                delta.changed_at,
            )
            for delta in deltas
        ]
        statement = sql.SQL(
            "INSERT INTO {} (uri, memory_type, field, search, replace, "
            "created, changed_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        ).format(self._table())
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(statement, rows)

    async def record(self, deltas: Sequence[DeltaLike]) -> None:
        """Append a batch of deltas, off the event loop.

        Named and shaped to match ov-ext's ``DeltaSink``: async, takes a batch,
        returns nothing. psycopg here is synchronous, and this is called from
        inside OpenViking's server while it is serving -- so the insert goes to
        a worker thread rather than stalling every other request behind a
        connection checkout.

        Parameters
        ----------
        deltas :
            What changed. An empty batch is a no-op and never leaves the loop.
        """
        if not deltas:
            return
        await asyncio.to_thread(self.record_sync, deltas)

    def pending(self, *, limit: int, since: datetime | None = None) -> list[DeltaRecord]:
        """Return deltas not yet reflected on, oldest first.

        Oldest first so a limit truncates the newest rows and the remainder is
        picked up next sweep. Newest first would strand everything below the cut
        until something else changed the same memory.

        Parameters
        ----------
        limit :
            Most rows to return.
        since :
            When given, only rows changed strictly after this. The sweep does
            not need it -- ``reflected_at`` already tracks what is done -- but a
            caller replaying a window can bound it.

        Returns
        -------
        list[DeltaRecord]
            Pending deltas, each carrying its ``id`` so the caller can mark it
            done.
        """
        where = sql.SQL("WHERE reflected_at IS NULL")
        params: list[Any] = []
        if since is not None:
            where = sql.SQL("WHERE reflected_at IS NULL AND changed_at > %s")
            params.append(since)
        statement = sql.SQL(
            "SELECT id, uri, memory_type, field, search, replace, created, "
            "changed_at FROM {} {} ORDER BY changed_at, id LIMIT %s"
        ).format(self._table(), where)
        params.append(limit)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params)
                columns = [column.name for column in cur.description or []]
                return [
                    DeltaRecord(zip(columns, row, strict=True)) for row in cur.fetchall()
                ]

    def mark_reflected(self, ids: Sequence[int], *, when: datetime) -> int:
        """Mark deltas as reflected on, and report how many changed.

        Called after the sweep's write succeeds, never before: a delta marked
        done by a sweep that then failed is a change nothing will ever reflect
        on.

        Parameters
        ----------
        ids :
            Row ids from :meth:`pending`.
        when :
            Timestamp to stamp. Passed in so the caller owns the clock and
            tests stay deterministic.

        Returns
        -------
        int
            Rows updated. Fewer than ``ids`` means some were already marked,
            which is what a re-run of an interrupted sweep looks like.
        """
        if not ids:
            return 0
        statement = sql.SQL(
            "UPDATE {} SET reflected_at = %s WHERE id = ANY(%s) AND reflected_at IS NULL"
        ).format(self._table())
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, (when, list(ids)))
                return cur.rowcount

    def remap_uri(self, old_uri: str, new_uri: str) -> int:
        """Move a memory's deltas onto the URI that replaced it.

        OpenViking merges two memories by deleting one and naming the other its
        replacement. Without this the dead URI keeps its history and the
        survivor loses it, so one memory's changes end up split across two keys
        and neither tells the whole story.

        Parameters
        ----------
        old_uri :
            The memory that was deleted.
        new_uri :
            The memory that replaced it.

        Returns
        -------
        int
            Rows moved.
        """
        if old_uri == new_uri:
            return 0
        statement = sql.SQL("UPDATE {} SET uri = %s WHERE uri = %s").format(self._table())
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, (new_uri, old_uri))
                return cur.rowcount

    def forget(self, uri: str) -> int:
        """Drop every delta for a memory, and report how many went.

        For a memory deleted outright rather than merged: its changes describe
        text that no longer exists anywhere, and reflection quoting them would
        cite a memory nobody can read.

        Returns
        -------
        int
            Rows removed.
        """
        statement = sql.SQL("DELETE FROM {} WHERE uri = %s").format(self._table())
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, (uri,))
                return cur.rowcount
