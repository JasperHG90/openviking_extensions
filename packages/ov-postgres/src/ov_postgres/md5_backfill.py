"""Fill in ``md5`` on rows that predate it, the way OpenViking computes it.

OpenViking 0.4.18 added an ``md5`` field to each vector record: for a file
(level 2), the md5 of the file's stored bytes; for a directory's abstract and
overview (levels 0 and 1), the md5 of the sidecar's previewable body. Its
maintenance reindex (``vectors_only``, the default) compares that against the
file it reads and re-embeds every record whose md5 differs -- an empty one
included. A table that gains the column through the schema reconcile therefore
has an empty md5 on every row, and the first reindex after the upgrade would
re-embed everything. (The add-resource diff copes with an empty md5 by
comparing bytes, and fills it in itself, so only reindex pays.)

The bytes live in OpenViking's filesystem, not in PostgreSQL: ``content``
holds the embedded text, which is not the file's bytes. So the backfill reads
each file through the server's own ``VikingFS`` -- which also decrypts it when
encryption at rest is on -- and hashes it with OpenViking's own functions,
``content_md5`` and ``body_for_preview``. That is why it runs inside the server
after it has started, and not from this package alone: see
:meth:`ov_postgres.adapter.PgVectorCollectionAdapter.run_md5_backfill`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

__all__ = ["BATCH_SIZE", "SIDECAR_NAMES", "FileReader", "md5_for", "run_md5_backfill"]

logger = logging.getLogger(__name__)

# Rows fingerprinted per round trip to the database.
BATCH_SIZE = 200

# The sidecar file that carries each directory level's embedding source.
SIDECAR_NAMES: dict[int, str] = {0: ".abstract.md", 1: ".overview.md"}


class FileReader(Protocol):
    """The one ``VikingFS`` method the backfill calls."""

    async def read_file_bytes(self, uri: str, ctx: object = None) -> bytes:
        """Return a file's stored bytes, decrypted."""
        ...


class _Collection(Protocol):
    """The collection methods the backfill uses."""

    def md5_backfill_pending(self) -> bool:
        """Whether the backfill still has to run."""
        ...

    def md5_backfill_batch(
        self, after: object, limit: int
    ) -> list[tuple[object, str, int, str]]:
        """Return the next rows with an empty md5."""
        ...

    def record_md5(self, key: object, md5: str) -> bool:
        """Set one row's md5 if it is still empty."""
        ...

    def finish_md5_backfill(self) -> None:
        """Clear the pending mark."""
        ...


async def md5_for(
    viking_fs: FileReader, uri: str, level: int, account_id: str
) -> str | None:
    """Return the md5 OpenViking would store for this record, or ``None``.

    Mirrors ``openviking.storage.resource_rfv.build_rfv_snapshot``: a file is
    hashed whole; a directory level is hashed as the previewable body of its
    sidecar, and has no md5 when that body is blank or the sidecar is absent.

    Parameters
    ----------
    viking_fs :
        The server's ``VikingFS``.
    uri :
        The record's ``viking://`` URI, without a level suffix.
    level :
        0 or 1 for a directory's sidecars, 2 for a file.
    account_id :
        The account the record belongs to, which scopes the read.

    Returns
    -------
    str | None
        The md5, or ``None`` when OpenViking would store none or the source
        cannot be read.
    """
    from openviking.server.identity import RequestContext, Role, UserIdentifier
    from openviking.storage.abstract_overview import body_for_preview
    from openviking.utils.content_hash import content_md5

    ctx = RequestContext(user=UserIdentifier(account_id, "default"), role=Role.ROOT)
    source = uri if level == 2 else f"{uri.rstrip('/')}/{SIDECAR_NAMES[level]}"
    try:
        raw = await viking_fs.read_file_bytes(source, ctx=ctx)
    except Exception as exc:
        # Absent or unreadable: leave it empty. OpenViking treats an empty md5
        # as unknown, which is what it is.
        logger.debug("ov_postgres: md5 backfill cannot read %s (%s)", source, exc)
        return None
    if level == 2:
        return str(content_md5(raw))
    try:
        body = body_for_preview(raw)
    except Exception as exc:
        logger.debug("ov_postgres: md5 backfill cannot parse %s (%s)", source, exc)
        return None
    if not body.strip():
        return None
    return str(content_md5(body.encode("utf-8")))


async def run_md5_backfill(
    collection: _Collection,
    viking_fs: FileReader,
    decode_uri: Callable[[str], object],
    *,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Fingerprint every row left with an empty md5, then clear the mark.

    Does nothing when no reconcile has marked the table. A row whose source
    cannot be read keeps its empty md5. A row written meanwhile with an md5 of
    its own keeps that one.

    Parameters
    ----------
    collection :
        The collection to fill in.
    viking_fs :
        The server's ``VikingFS``.
    decode_uri :
        Turns a stored URI into the ``viking://`` form.
    batch_size :
        Rows read per round trip.

    Returns
    -------
    int
        How many rows were given an md5.
    """
    if not await asyncio.to_thread(collection.md5_backfill_pending):
        return 0
    filled = 0
    skipped = 0
    after: object = None
    while True:
        batch = await asyncio.to_thread(collection.md5_backfill_batch, after, batch_size)
        for key, stored_uri, level, account_id in batch:
            uri = str(decode_uri(stored_uri))
            md5 = await md5_for(viking_fs, uri, level, account_id) if account_id else None
            if md5 is None:
                skipped += 1
                continue
            if await asyncio.to_thread(collection.record_md5, key, md5):
                filled += 1
        if len(batch) < batch_size:
            break
        after = batch[-1][0]
    await asyncio.to_thread(collection.finish_md5_backfill)
    logger.info(
        "ov_postgres: md5 backfill done; %d row(s) filled, %d left empty because "
        "their source could not be read or has no body",
        filled,
        skipped,
    )
    return filled
