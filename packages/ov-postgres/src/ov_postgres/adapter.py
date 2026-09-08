"""``CollectionAdapter`` binding OpenViking to PostgreSQL + pgvector.

Selected by pointing the vectordb backend at this class::

    "vectordb": {
      "backend": "ov_postgres.adapter.PgVectorCollectionAdapter",
      "name": "context",
      "custom_params": {"dsn": "postgresql://user:pw@host:5432/openviking"}
    }

``VectorDBBackendConfig.validate_config`` skips its allow-list for any backend
string containing a dot, and ``create_collection_adapter`` imports it -- so no
change to the installed package is required.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Sequence
from typing import Any

from openviking.storage.vectordb.collection.collection import Collection
from openviking.storage.vectordb_adapters.base import CollectionAdapter
from psycopg import sql
from psycopg_pool import ConnectionPool

from . import ddl
from .collection import PgVectorCollection
from .config import DSN_ENV_VARS, PgVectorParams, VectorDBConfigLike, resolve_dsn
from .observability import DB_SYSTEM, annotate, traced
from .schema import CollectionSchema

logger = logging.getLogger(__name__)

__all__ = ["DSN_ENV_VARS", "PgVectorCollectionAdapter"]

_IDENT_SAFE = re.compile(r"[^a-z0-9_]+")

# PostgreSQL truncates identifiers at 63 bytes.
_MAX_IDENTIFIER_LENGTH = 63


class PgVectorCollectionAdapter(CollectionAdapter):  # type: ignore[misc]  # base is untyped
    """Adapter for a PostgreSQL/pgvector-backed collection.

    Parameters
    ----------
    collection_name :
        Name of the OpenViking collection this adapter is bound to.
    index_name :
        Name of the index bundle recorded in the registry.
    dsn :
        libpq connection string.
    params :
        Validated backend settings.
    sparse_weight :
        Weight applied to the sparse term in hybrid scoring. Zero disables
        sparse contribution entirely. Comes from the parent config rather than
        ``custom_params``, since OpenViking owns it.

    Attributes
    ----------
    mode : str
        Backend identifier reported to OpenViking; always ``"pgvector"``.
    USE_CONTENT_FIELD : bool
        Whether OpenViking should write each record's body text to the
        ``content`` column. Taken from ``params.store_content``.
    """

    # Declared because the base class is untyped, so mypy cannot otherwise
    # infer the attribute it sets in __init__.
    _collection: Collection | None

    # The same collection as `_collection`, before OpenViking's facade wraps
    # it. Kept so this backend's own methods stay reachable.
    _pg_collection: PgVectorCollection | None

    # PostgreSQL has no per-statement row cap; batching uses executemany.
    _DATA_BATCH_SIZE: int | None = None

    # OpenViking reads this off the adapter to decide two things: whether to
    # materialise a record's body text at all
    # (``TextEmbeddingHandler._materialize_content``), and whether to keep it on
    # the payload or drop it (``_SingleAccountBackend._prepare_upsert_payload``).
    # Both read it through the instance, so ``__init__`` overrides it per
    # collection from ``store_content``.
    #
    # False by default. Full-text grep routing in
    # openviking/storage/viking_fs/_grep.py hard-codes ("volcengine",
    # "vikingdb"), so this backend is never chosen for server-side grep, and
    # content costs space that only keyword search can spend.
    USE_CONTENT_FIELD: bool = False

    # PostgreSQL refuses to build a tsvector from more than 1048575 bytes, and
    # the full-text index computes one on *every* write -- so an oversized body
    # aborts the INSERT itself, not merely its indexing. OpenViking caps
    # `content` at 1 MiB of *characters*, which is up to 4 MiB of UTF-8, so its
    # cap alone does not keep us under the limit.
    #
    # The base class truncates each field in ``_TRUNCATABLE_TEXT_FIELDS``
    # ("content", "abstract") to this many bytes, on a UTF-8 character
    # boundary. Both can land in one tsvector alongside the shorter keyword
    # columns, so the budget is set below half the limit rather than at it.
    # Applied whether or not `store_content` is on: a large `abstract` is in
    # the default keyword fields and could overflow the same index on its own.
    _TEXT_FIELD_BYTE_LIMIT: int = 448 * 1024

    def __init__(
        self,
        *,
        collection_name: str,
        index_name: str,
        dsn: str,
        params: PgVectorParams,
        sparse_weight: float = 0.0,
    ) -> None:
        super().__init__(collection_name=collection_name, index_name=index_name)
        self.mode = "pgvector"
        self._dsn = dsn
        self._params = params
        # Shadows the class attribute, so one collection can store bodies while
        # another does not. Set before anything can read it.
        self.USE_CONTENT_FIELD = params.store_content
        self._sparse_weight = sparse_weight
        self._pg_collection = None
        self._pool: ConnectionPool | None = None
        self._bootstrapped = False
        self._pgvector_version: tuple[int, ...] = ()
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, config: VectorDBConfigLike) -> PgVectorCollectionAdapter:
        """Build an adapter from an OpenViking vectordb config.

        ``custom_params`` is validated into :class:`PgVectorParams` here, so a
        malformed or misspelled option raises at startup rather than surfacing
        as a wrong default much later.

        Parameters
        ----------
        config :
            Any object carrying the attributes of
            :class:`~ov_postgres.config.VectorDBConfigLike`, in practice
            OpenViking's ``VectorDBBackendConfig``.

        Returns
        -------
        PgVectorCollectionAdapter
            An adapter that has not yet connected.

        Raises
        ------
        pydantic.ValidationError
            If ``custom_params`` contains an unknown or invalid option.
        ValueError
            If no connection string is configured.
        """
        raw_params = getattr(config, "custom_params", None) or {}
        params = PgVectorParams.model_validate(raw_params)

        dsn = resolve_dsn(params, getattr(config, "url", None))
        if params.distance is None:
            # The parent config owns the metric unless custom_params overrides.
            metric = getattr(config, "distance_metric", None) or "cosine"
            params = params.model_copy(update={"distance": metric})

        return cls(
            collection_name=getattr(config, "name", None) or "context",
            index_name=getattr(config, "index_name", None) or "default",
            dsn=dsn,
            params=params,
            sparse_weight=float(getattr(config, "sparse_weight", 0.0) or 0.0),
        )

    def _table_name(self) -> str:
        """Return the table name for the bound collection.

        Sanitising and truncating are both lossy: ``my-collection`` and
        ``my_collection`` sanitise alike, and two long names can truncate
        alike. Either would silently bind the second collection to the first
        one's table, so whenever the name is altered a short digest of the
        original is appended to keep it distinct.

        Returns
        -------
        str
            A PostgreSQL-safe table name of at most 63 bytes.
        """
        safe = _IDENT_SAFE.sub("_", self._collection_name.lower())
        candidate = f"{self._params.table_prefix}{safe}"
        if safe == self._collection_name and len(candidate) <= _MAX_IDENTIFIER_LENGTH:
            return candidate
        digest = hashlib.sha256(self._collection_name.encode("utf-8")).hexdigest()[:8]
        suffix = f"_{digest}"
        keep = _MAX_IDENTIFIER_LENGTH - len(suffix)
        return f"{candidate[:keep]}{suffix}"

    def _get_pool(self) -> ConnectionPool:
        """Return the connection pool, opening it on first use."""
        with self._lock:
            if self._pool is None:
                self._pool = ConnectionPool(
                    conninfo=self._dsn,
                    min_size=self._params.min_pool_size,
                    max_size=self._params.max_pool_size,
                    timeout=self._params.connect_timeout,
                    kwargs={"application_name": self._params.application_name},
                    open=True,
                )
            return self._pool

    @traced("ov_postgres.bootstrap")
    def _bootstrap(self) -> None:
        """Create the extension, helper functions and registry tables.

        Also records the installed pgvector version, which gates features that
        are not available on every release.

        Raises
        ------
        RuntimeError
            If ``create_extension`` is disabled and the extension is absent.
        """
        annotate(
            {
                "db.system.name": DB_SYSTEM,
                "db.namespace": self._params.db_schema,
                "ov_postgres.create_extension": self._params.create_extension,
            }
        )
        with self._lock:
            if self._bootstrapped:
                # Bootstrap runs once per adapter; later callers wait on the
                # lock and return. Worth distinguishing in a trace from the
                # call that did the work.
                annotate({"ov_postgres.outcome": "already_bootstrapped"})
                return
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                for statement in ddl.bootstrap_statements(
                    self._params.db_schema,
                    create_extension=self._params.create_extension,
                ):
                    cur.execute(statement)
                cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
                row = cur.fetchone()

            if row is None:
                # Only reachable with create_extension disabled; otherwise the
                # CREATE above would have raised first.
                raise RuntimeError(
                    "The 'vector' extension is not installed in this database and "
                    "custom_params.create_extension is false. Ask an administrator "
                    "to run CREATE EXTENSION vector, or set create_extension to true "
                    "if the role has the privilege."
                )
            self._pgvector_version = ddl.parse_extension_version(str(row[0]))
            logger.debug("pgvector version detected: %s", self._pgvector_version)
            annotate(
                {
                    "ov_postgres.pgvector_version": str(row[0]),
                    "ov_postgres.outcome": "bootstrapped",
                }
            )
            self._bootstrapped = True

    @traced("ov_postgres.load_collection")
    def _load_existing_collection_if_needed(self) -> None:
        """Bind to an already-created collection, if the registry knows one."""
        annotate(
            {
                "db.system.name": DB_SYSTEM,
                "db.namespace": self._params.db_schema,
                "db.operation.name": "SELECT",
            }
        )
        with self._lock:
            if self._collection is not None:
                annotate({"ov_postgres.outcome": "already_loaded"})
                return
            self._bootstrap()
            pool = self._get_pool()
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT table_name, meta FROM {}.{} WHERE name = %s").format(
                        sql.Identifier(self._params.db_schema),
                        sql.Identifier(ddl.REGISTRY_COLLECTIONS),
                    ),
                    (self._collection_name,),
                )
                row = cur.fetchone()
            if not row:
                return

            table_name, meta = row[0], row[1]
            if not isinstance(meta, dict):
                meta = json.loads(meta)

            # A registry row whose table was dropped out of band means the
            # collection does not exist; report that rather than failing later
            # on every query.
            if not self._table_exists(table_name):
                logger.warning(
                    "pgvector registry lists collection %s but table %s.%s is missing",
                    self._collection_name,
                    self._params.db_schema,
                    table_name,
                )
                return

            self._collection = self._build_collection(
                CollectionSchema.from_meta(meta), table_name
            )

    @traced("ov_postgres.create_collection")
    def _create_backend_collection(self, meta: dict[str, Any]) -> Collection:
        """Create the collection table and record it in the registry.

        Parameters
        ----------
        meta :
            The OpenViking collection schema.

        Returns
        -------
        Collection
            A handle wrapping the new table.
        """
        annotate(
            {
                "db.system.name": DB_SYSTEM,
                "db.namespace": self._params.db_schema,
                "db.operation.name": "CREATE TABLE",
            }
        )
        self._bootstrap()
        coll_schema = CollectionSchema.from_meta(meta)
        table_name = self._table_name()
        pool = self._get_pool()

        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl.create_table(self._params.db_schema, table_name, coll_schema))
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}.{} (name, table_name, meta) VALUES (%s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET "
                    "table_name = EXCLUDED.table_name, meta = EXCLUDED.meta"
                ).format(
                    sql.Identifier(self._params.db_schema),
                    sql.Identifier(ddl.REGISTRY_COLLECTIONS),
                ),
                (self._collection_name, table_name, json.dumps(meta)),
            )

        return self._build_collection(coll_schema, table_name)

    def _table_exists(self, table_name: str) -> bool:
        """Return whether the named table is present in the configured schema."""
        pool = self._get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass(%s) IS NOT NULL",
                (f"{self._params.db_schema}.{table_name}",),
            )
            row = cur.fetchone()
        return bool(row and row[0])

    def _stored_index_distance(self) -> str | None:
        """Return the distance metric the index bundle was created with.

        Returns
        -------
        str | None
            The stored metric, or ``None`` when no bundle is registered.
        """
        pool = self._get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT meta FROM {}.{} WHERE collection = %s AND index_name = %s"
                ).format(
                    sql.Identifier(self._params.db_schema),
                    sql.Identifier(ddl.REGISTRY_INDEXES),
                ),
                (self._collection_name, self._index_name),
            )
            row = cur.fetchone()
        if not row:
            return None
        meta = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        distance = (meta.get("VectorIndex") or {}).get("Distance")
        return str(distance) if distance else None

    def pairwise_similarity(
        self, values: Sequence[object], *, field: str | None = None
    ) -> dict[tuple[object, object], float]:
        """Return cosine similarity between every pair of the given rows.

        Reaches past OpenViking's ``Collection`` facade, which forwards only
        the methods of the backend interface and so cannot see this one. A
        diversity pass such as MMR calls it to compare candidates without
        pulling their embeddings across the wire.

        Parameters
        ----------
        values :
            Values identifying the candidate rows.
        field :
            Column those values name; ``None`` means the primary key. Callers
            working in URIs pass ``"uri"``.

        Returns
        -------
        dict[tuple[object, object], float]
            Similarity by value pair, in both orderings. Empty when the adapter
            has no open collection, so a caller can treat "cannot" and
            "nothing similar" alike and skip the diversity pass.
        """
        inner = self._pg_collection
        if inner is None:
            return {}

        # A URI is stored with its `viking://` scheme stripped -- see
        # `_encode_uri_field_value` -- and handed back with it restored. A
        # caller passing what a search returned would therefore match no row
        # at all, and the diversity pass this feeds would quietly do nothing
        # rather than fail. Encode on the way in and decode on the way out, so
        # the caller works in the URIs it was given.
        if field in self._URI_FIELD_NAMES:
            lookup = [self._encode_uri_field_value(value) for value in values]
            matrix = inner.pairwise_similarity(lookup, field=field)
            return {
                (
                    self._decode_uri_field_value(left),
                    self._decode_uri_field_value(right),
                ): score
                for (left, right), score in matrix.items()
            }
        return inner.pairwise_similarity(values, field=field)

    def _build_collection(
        self, coll_schema: CollectionSchema, table_name: str
    ) -> Collection:
        """Wrap a :class:`PgVectorCollection` in OpenViking's Collection facade."""
        # An ANN index is built for one operator class. Querying with a
        # different metric than the index was created with would make the index
        # unusable and rank by something other than what was asked for, so the
        # stored metric wins over the configured default.
        distance = self._stored_index_distance() or self._params.distance or "cosine"
        # Held directly as well as inside the facade: the facade forwards only
        # the backend interface, so anything extra this backend offers would
        # otherwise be unreachable.
        self._pg_collection = PgVectorCollection(
            pool=self._get_pool(),
            db_schema=self._params.db_schema,
            collection_name=self._collection_name,
            table_name=table_name,
            coll_schema=coll_schema,
            distance=distance,
            sparse_weight=self._sparse_weight,
            index_method=self._params.index_method,
            index_options=self._params.index_options,
            keyword_fields=self._params.resolved_keyword_fields(),
            keyword_query_mode=self._params.keyword_query_mode,
            keyword_rank=self._params.keyword_rank,
            text_search_config=self._params.text_search_config,
            tz_policy=self._params.tz_policy,
            iterative_scan=self._params.iterative_scan,
            pgvector_version=self._pgvector_version,
            index_name=self._index_name,
            owns_pool=False,
        )
        return Collection(self._pg_collection)

    def update_data(self, data_list: list[dict[str, Any]]) -> list[str]:
        """Apply partial updates, matching ``LocalCollectionAdapter``.

        Parameters
        ----------
        data_list :
            Records to update; each must carry the primary key.

        Returns
        -------
        list[str]
            Primary keys of the rows that were actually updated.
        """
        result = self.get_collection().update_data(data_list)
        return list(getattr(result, "ids", None) or [])

    def backfill_defaults(self, *, batch_size: int = 5000) -> int:
        """Repair rows written before per-type defaults were applied.

        Exposed here because OpenViking's ``Collection`` facade forwards only
        the ``ICollection`` interface, so the underlying method is otherwise
        reachable only through a name-mangled attribute.

        Parameters
        ----------
        batch_size :
            Rows to repair per transaction.

        Returns
        -------
        int
            Number of rows updated.
        """
        collection = self.get_collection()
        inner: PgVectorCollection = collection._Collection__collection
        return inner.backfill_defaults(batch_size=batch_size)

    def ensure_indexes(self) -> list[str]:
        """Create any index this version expects but the database lacks.

        OpenViking skips ``create_index`` for a collection that already exists,
        so a database created by an earlier version of this package keeps the
        indexes it had. Run this once after upgrading.

        Returns
        -------
        list[str]
            Names of indexes that were missing before this ran.
        """
        collection = self.get_collection()
        inner: PgVectorCollection = collection._Collection__collection
        return inner.ensure_indexes()

    def close(self) -> None:
        """Release the collection handle and close the connection pool."""
        super().close()
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close()
                finally:
                    self._pool = None
            self._bootstrapped = False
