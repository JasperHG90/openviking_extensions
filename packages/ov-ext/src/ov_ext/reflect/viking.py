"""The one place reflection touches OpenViking.

Everything OpenViking-shaped lives behind :class:`VikingStore` and
:class:`VikingLLM`, which satisfy the protocols in :mod:`ov_ext.reflect.ports`
structurally -- OpenViking knows nothing about this package, so nothing is
inherited and the dependency points one way.

Reads run against the vector index, never the document store. The L2 rows
already carry the memory text -- OpenViking writes ``strip_all_links(content)``
into them at index time -- so a sweep can find its changed memories, gather
neighbours and read every quote it verifies without opening a single file.
Only two operations reach the document store, and only for conclusions: writing
an observation, and merging its ``derived_from`` edges back onto it.

Reflection writes observations with ``write_file`` rather than through
``remember``. ``remember`` hands text to the extractor, which would rewrite the
observation into whatever it decided the text meant -- the opposite of the
point. A memory file written directly is still a first-class memory: it is
indexed, searchable and linkable, and OpenViking's own ``MemoryFileUtils``
serializes it so the format is theirs rather than ours.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel

from .citations import parse_timestamp
from .config import ReflectSettings
from .exceptions import ContentUnavailableError
from .models import MemoryRow, Observation
from .verify import normalise

__all__ = ["VikingLLM", "VikingStore"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Fields the change query asks for. Deliberately not `content`: this query runs
# over every memory in the store, and the text is fetched later for the handful
# that end up in a batch.
_CHANGE_FIELDS = ["uri", "updated_at"]

# Fields a row needs to become a MemoryRow. `abstract` is deliberately absent:
# quotes are verified against whatever text a row carries, and a summary is not
# the memory.
_ROW_FIELDS = ["uri", "content", "created_at", "updated_at"]

# How much wider than the requested sample to read before choosing at random.
# Wide enough that two sweeps rarely draw the same rows, narrow enough that the
# query stays a cheap indexed scan.
_TAIL_WINDOW = 10

# Between two changes to one memory in a batch. Verification normalises
# whitespace before comparing, so joining with a newline would let a quote run
# from the end of one change into the start of another -- a span that exists in
# no memory, verified against a row that only looks like one. The rule is a
# token normalisation cannot collapse away.
# How much wider than the requested batch to read before filtering by memory
# type. Most deltas in a busy store are entities, so a small factor is enough.
_DELTA_OVERREAD = 4

# How much wider than needed to read before the evidence filter runs. A search
# over the whole user root returns preferences and peer memories too, and one
# discarded should not cost a neighbour its slot.
_EVIDENCE_OVERREAD = 3

_DELTA_SEPARATOR = "\n\n---\n\n"


class VikingLLM:
    """OpenViking's structured-output model, behind the reflection protocol.

    Wraps ``openviking_cli.utils.llm.StructuredLLM``, which appends the JSON
    schema to the prompt, calls the model and parses the reply. That class
    rather than ``openviking.models.vlm.llm.StructuredVLM``: the latter builds
    its own client from a config dict, and an empty dict defaults it to OpenAI
    (``VLMFactory.create`` in ``models/vlm/base.py``). The CLI one resolves
    ``get_openviking_config().vlm``, which is the model the server was actually
    configured with -- so one set of credentials, one timeout policy and one
    trace, in a process that is already OpenViking's.

    Parameters
    ----------
    llm :
        Something with ``complete_json_async(prompt, schema=...)``. Built from
        OpenViking's configured model when omitted.
    settings :
        Behaviour toggles. Read for ``model``, which overrides the model the
        server was configured with -- reflection needs no vision and does need
        to finish, and those are not the same model.
    """

    def __init__(
        self, llm: Any | None = None, settings: ReflectSettings | None = None
    ) -> None:
        self._llm = llm
        self._settings = settings or ReflectSettings()
        # The reconfigured config, built once. `_get_vlm` is called per request,
        # so building per call would hand every model call its own HTTP client
        # and pay a fresh handshake -- on the subsystem whose founding symptom
        # was calls timing out.
        self._own_config: Any = None

    def _get_llm(self) -> Any:
        """Return the wrapped model, building OpenViking's default on first use."""
        if self._llm is None:
            from openviking_cli.utils.llm import StructuredLLM

            llm = StructuredLLM()
            chosen = self._settings.model.strip()
            if chosen:
                llm._get_vlm = lambda: self._reconfigured(chosen)
            self._llm = llm
        return self._llm

    def _reconfigured(self, model: str) -> Any:
        """Return the config the sweep runs against, building it at most once."""
        if self._own_config is None:
            self._own_config = self._own_vlm(model)
        return self._own_config

    def _own_vlm(self, model: str) -> Any:
        """Build a config for ``model``, on the server's credentials.

        Returns the ``VLMConfig``, not the client it builds, because that is
        what ``StructuredLLM`` expects back: it calls
        ``_get_vlm().get_completion_async(prompt)``, and the config's wrapper is
        what injects ``thinking``. Handing back the raw client instead drops
        that setting for reflection alone, and skips the config's own instance
        cache so every call builds a fresh HTTP client.

        The server's ``VLMConfig`` is copied and its ``model`` swapped, so the
        endpoint, key, timeout and retry policy are the ones the deployment was
        configured with -- only the model differs. Building a config from
        scratch would default to OpenAI, which is how
        ``StructuredVLM``'s own constructor goes wrong.

        The copy is shallow and its cached instance cleared. Shallow because a
        deep copy cannot walk the cached client's thread lock; cleared because
        otherwise ``get_vlm_instance()`` hands back the instance the server
        already built for the old name, and reflection quietly keeps using it.

        Falls back to the server's model when the copy or the build fails: a
        sweep on the wrong model is worth more than a sweep that cannot start.
        A *missing* OpenViking config is not caught, because there is then no
        server model to fall back to and nothing else in the process works
        either -- the same error the unmodified ``StructuredLLM`` would raise.
        """
        from openviking_cli.utils.config import get_openviking_config

        configured = get_openviking_config().vlm
        try:
            # Shallow. A deep copy walks `__pydantic_private__`, which holds
            # the cached client -- and that holds a thread lock, so deepcopy
            # raises `cannot pickle '_thread.lock'` and the override silently
            # falls back to the server's model. Nothing nested is mutated here;
            # only `model` is, and that is a string on the copy.
            own = configured.model_copy()
            own.model = model
            # Credentials win over the top-level name:
            # `_build_vlm_config_dict_for_credential` takes
            # `credential.model or self.model`, so a config with credentials --
            # which is what OpenViking writes -- ignores `own.model` entirely
            # and keeps building the server's. Replaced rather than mutated, so
            # the server's own credentials are untouched.
            if own.credentials:
                own.credentials = [
                    credential.model_copy(update={"model": model})
                    for credential in own.credentials
                ]
            # A pydantic private, so `object.__setattr__` writes a shadow the
            # real accessor never reads and `get_vlm_instance()` returns None.
            # `_vlm_instance` on the copy is a *different* dict entry from the
            # server's -- pydantic's `__copy__` rebuilds `__pydantic_private__`
            # rather than sharing it -- so clearing it here cannot clear the
            # server's cache. That is the property the whole approach rests on.
            own.__pydantic_private__["_vlm_instance"] = None
        except Exception:
            logger.exception(
                "ov-ext reflect: could not build %r; falling back to the server's model",
                model,
            )
            return configured
        logger.info("ov-ext reflect: reasoning with %s", model)
        return own

    async def complete(self, prompt: str, model: type[T]) -> T | None:
        """Answer ``prompt`` as an instance of ``model``, or ``None``."""
        schema = model.model_json_schema()
        payload = await self._get_llm().complete_json_async(prompt, schema=schema)
        if payload is None:
            return None
        try:
            return model.model_validate(payload)
        except ValueError:
            # The model answered in a shape that is not this one. Treated as a
            # non-answer rather than an error: one unparseable reply should
            # cost a batch, not the sweep.
            logger.warning("ov-ext reflect: reply did not validate as %s", model.__name__)
            return None


class VikingStore:
    """Reflection's view of OpenViking, behind the reflection protocol.

    Parameters
    ----------
    viking_fs :
        OpenViking's filesystem, for reads of generated overviews and for
        writes.
    vikingdb :
        The vector store manager, for every indexed read.
    ctx :
        Request context carrying the user and their permissions. Every call
        here is scoped by it.
    settings :
        Behaviour toggles, for the roots reflection reads and writes under.
    rng :
        Source of randomness for the tail sample. Injectable so a test can make
        it deterministic; left to the system otherwise, because varying between
        sweeps is the point.
    deltas :
        Captured changes, when the backend is recording them. This is the whole
        point of the delta work: with it, a changed memory is shown to the model
        as the lines that changed rather than the twenty kilobytes around them.
        Without it the sweep reads whole memories, which is what it always did.
    """

    def __init__(
        self,
        viking_fs: Any,
        vikingdb: Any,
        ctx: Any,
        settings: ReflectSettings | None = None,
        rng: random.Random | None = None,
        deltas: Any | None = None,
    ) -> None:
        self._fs = viking_fs
        self._db = vikingdb
        self._ctx = ctx
        self._settings = settings or ReflectSettings()
        self._deltas = deltas
        # Row ids consumed by the current sweep, so a completed batch can retire
        # exactly what it read. Keyed by URI because that is what the engine
        # hands back.
        self._consumed: dict[str, list[int]] = {}
        # The delta rows behind each URI in the current batch, so `rows`
        # can build a memory whose text is the change rather than the file.
        self._delta_text: dict[str, list[dict[str, Any]]] = {}
        # Unseeded on purpose: the tail sample exists to vary between sweeps,
        # so reproducibility here would defeat it. Tests inject their own.
        self._rng = rng or random.Random()

    @property
    def _memory_root(self) -> str:
        """Root under which this user's memories live."""
        return f"viking://user/{self._ctx.user.user_id}/memories"

    @property
    def reads_deltas(self) -> bool:
        """Whether this store is reading captured changes, not whole memories."""
        return self._deltas is not None

    @property
    def _user_root(self) -> str:
        """Root covering memories and resources alike.

        Evidence is drawn from here rather than from ``_memory_root``, because
        a resource lives at ``viking://user/<id>/resources/`` -- outside the
        memory root entirely, so a search scoped there could never reach one.
        """
        return f"viking://user/{self._ctx.user.user_id}"

    def _is_evidence(self, uri: str) -> bool:
        """Whether a URI may be drawn on as evidence.

        Keyed on the segment after ``memories/`` or on ``resources`` -- the
        index reports ``context_type`` as ``memory`` for every memory type
        alike, so it cannot tell an entity from a preference and the URI has to.
        """
        allowed = self._settings.evidence_types
        root = self._user_root
        if uri.startswith(f"{root}/resources/"):
            return "resources" in allowed
        prefix = f"{root}/memories/"
        if not uri.startswith(prefix):
            # Everything else, and peer vaults in particular. A peer's memories
            # are another person's, and writing findings about someone who never
            # asked to be reflected on is not ours to do.
            return False
        return uri[len(prefix) :].split("/", 1)[0] in allowed

    def _row_from_record(self, record: dict[str, Any]) -> MemoryRow | None:
        """Build a :class:`MemoryRow` from an index record, or ``None``.

        Reads ``content`` and nothing else. There is deliberately no fallback
        to ``abstract``: quotes are verified against whatever text this
        returns, and verifying against a generated summary would produce links
        whose ``match_text`` is absent from the memory they point at. A row
        without content is skipped, and :meth:`rows` turns a whole batch of
        them into a refusal.
        """
        uri = record.get("uri")
        text = record.get("content")
        if not uri or not text:
            return None
        return MemoryRow(
            uri=str(uri),
            text=str(text),
            created_at=parse_timestamp(record.get("created_at")),
            updated_at=parse_timestamp(record.get("updated_at")),
        )

    async def changed_since(self, moment: datetime, *, limit: int) -> list[str]:
        """Return stored URIs of memory rows updated after ``moment``.

        Reads the delta table when one is configured. The watermark is not
        consulted then: a delta is pending until the sweep that consumed it
        retires it, which is a better record of what is outstanding than a
        timestamp that cannot express "read but not finished".
        """
        if self._deltas is not None:
            return await self._changed_from_deltas(limit=limit)

        from openviking.storage.expr import And, Eq, PathScope, TimeRange

        # level=2 is the memory itself. Levels 0 and 1 are the generated
        # directory abstract and overview, whose refresh deliberately lags
        # their contents -- using them as a change signal would miss changes in
        # exactly the large directories that matter most.
        condition = And(
            [
                PathScope("uri", self._memory_root),
                Eq("context_type", "memory"),
                Eq("level", 2),
                # ISO string, not a datetime: the backends coerce a
                # `date_time` operand through `parse_datetime_to_epoch_ms`,
                # which accepts a string or a number and rejects everything
                # else outright.
                TimeRange("updated_at", start=moment.isoformat()),
            ]
        )
        # Ascending, so `limit` truncates the *newest* rows rather than the
        # oldest. The watermark then advances only as far as this batch reached
        # and the remainder is picked up next sweep. Descending would strand
        # everything below the cut permanently.
        #
        # TimeRange compiles `start` to `>=`, not `>`, so the row sitting
        # exactly on the mark comes back every sweep. Harmless on its own --
        # re-reflecting one memory is idempotent -- and dropped below so it
        # does not spend a batch slot. It must NOT be filtered in SQL: rows
        # written in the same second as the mark would be lost with it.
        records = await self._db.filter(
            filter=condition,
            limit=limit,
            output_fields=_CHANGE_FIELDS,
            order_by="updated_at",
            order_desc=False,
            ctx=self._ctx,
        )
        return [
            str(record["uri"])
            for record in records
            if record.get("uri") and parse_timestamp(record.get("updated_at")) > moment
        ]

    async def _changed_from_deltas(self, *, limit: int) -> list[str]:
        """Return URIs with pending deltas, oldest change first.

        Records which rows each URI accounts for, so :meth:`mark_reflected` can
        retire exactly what was read rather than everything that happens to be
        pending when the batch finishes.
        """
        reader = self._deltas
        assert reader is not None  # only reached with a store configured
        # Over-read, because the filter below discards rows: asking for exactly
        # `limit` would return a batch of preferences and call it a full sweep.
        pending = await asyncio.to_thread(reader.pending, limit=limit * _DELTA_OVERREAD)
        wanted = set(self._settings.memory_types)
        self._consumed = {}
        self._delta_text = {}
        ordered: list[str] = []
        skipped = 0
        for record in pending:
            uri = str(record["uri"])
            if str(record.get("memory_type") or "") not in wanted:
                # Read and retired without reflection: the change is real, it is
                # just not something an observation should be drawn from. Leaving
                # it pending would make the sweep read it again every tick.
                self._consumed.setdefault(uri, []).append(int(record["id"]))
                skipped += 1
                continue
            if len(ordered) >= limit and uri not in self._consumed:
                break
            if uri not in self._consumed:
                self._consumed[uri] = []
                ordered.append(uri)
            self._consumed[uri].append(int(record["id"]))
            self._delta_text.setdefault(uri, []).append(record)
        if skipped:
            logger.debug(
                "ov-ext reflect: %d deltas outside %s retired without reflection",
                skipped,
                sorted(wanted),
            )
            await self.mark_reflected(
                [uri for uri in self._consumed if uri not in self._delta_text]
            )
        return ordered

    async def mark_reflected(self, uris: Sequence[str]) -> None:
        """Retire the deltas the given URIs accounted for."""
        if self._deltas is None:
            return
        ids = [row_id for uri in uris for row_id in self._consumed.get(uri, [])]
        if not ids:
            return
        await asyncio.to_thread(
            self._deltas.mark_reflected, ids, when=datetime.now(timezone.utc)
        )
        for uri in uris:
            self._consumed.pop(uri, None)
            self._delta_text.pop(uri, None)

    def _row_from_deltas(self, uri: str) -> MemoryRow | None:
        """Build a row whose text is what changed, not the whole memory.

        Only the ``replace`` sides. A delta's ``search`` side is the text that
        was *removed*, and showing it would let the model quote it: verification
        would pass, because the quote really is in the row it was shown, and the
        resulting ``derived_from`` link would carry a ``match_text`` the cited
        memory no longer contains. OpenViking renders a link by finding that
        span (``LinkRenderer._find_match_span``), so it would render nothing,
        and the observation would rest on evidence the store cannot show anyone.
        A deletion is real information and this drops it; citing a retracted
        line is worse than missing it.

        Returns ``None`` when nothing citable is left, which is what a batch of
        pure deletions looks like.
        """
        records = self._delta_text.get(uri)
        if not records:
            return None
        lines = [str(record["replace"] or "").strip() for record in records]
        text = _DELTA_SEPARATOR.join(line for line in lines if line)
        if not text:
            return None
        newest = max(parse_timestamp(record["changed_at"]) for record in records)
        oldest = min(parse_timestamp(record["changed_at"]) for record in records)
        return MemoryRow(uri=uri, text=text, created_at=oldest, updated_at=newest)

    def group(self, uris: Sequence[str]) -> dict[str, list[str]]:
        """Split a sweep into batches.

        One batch when reading deltas. Directory grouping was sized for whole
        memories, where a directory's worth was already a full prompt; a delta
        is a line, so the same grouping scatters a sweep into batches of one or
        two -- and an observation must cite two distinct memories, so most of
        them could not produce anything. Measured: the same six changes gave 0
        observations across four directory batches and 2 in a single batch.

        The key is the label the caller looks an overview up by. A pooled batch
        spans directories, so there is no one overview to fetch and the prompt
        runs without that background.
        """
        from .engine import group_by_directory

        if self._deltas is not None:
            return {"": list(uris)}
        return group_by_directory(uris)

    async def rows(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Fetch the text and timestamps for specific URIs.

        Raises
        ------
        ContentUnavailableError
            When rows came back but none carried ``content``. That means the
            backend is not storing memory text, so no quote could be verified
            against the memory it cites -- see the exception for why falling
            back to ``abstract`` would be worse than stopping.
        """
        if not uris:
            return []
        if self._deltas is not None:
            # Per URI, not per call. `if any_deltas: return them` looks the same
            # until one neighbour in a batch happens to have a pending change,
            # at which point every other neighbour silently vanishes from the
            # evidence pool and `min_evidence` starves while the sweep looks
            # healthy.
            from_deltas = {
                uri: row
                for uri, row in ((uri, self._row_from_deltas(uri)) for uri in uris)
                if row is not None
            }
            # Capped like any other row. "A changed memory is delta-sized
            # already" holds for a line edit and not for a create, which records
            # the whole new body as one `replace` -- a dozen of those rebuilds
            # the prompt this work exists to shrink.
            from_deltas = dict(
                zip(
                    from_deltas,
                    self._as_context(list(from_deltas.values())),
                    strict=True,
                )
            )
            remaining = [uri for uri in uris if uri not in from_deltas]
            if not remaining:
                return list(from_deltas.values())
            indexed = await self._rows_from_index(remaining)
            # Delta rows first, so the changed memories keep the low citation
            # indices the prompt treats as the subject.
            return list(from_deltas.values()) + indexed
        return await self._rows_from_index(uris)

    async def _rows_from_index(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Fetch whole memories for ``uris`` from the vector index.

        The path taken when nothing captured a change for a memory -- every
        memory before deltas were switched on, and every neighbour drawn in for
        context.

        Raises
        ------
        ContentUnavailableError
            When rows came back but none carried ``content``.
        """
        from openviking.storage.expr import In

        if not uris:
            return []
        records = await self._db.filter(
            filter=In("uri", list(uris)),
            limit=len(uris),
            output_fields=_ROW_FIELDS,
            ctx=self._ctx,
        )
        records = list(records)
        rows = [
            row
            for row in (self._row_from_record(record) for record in records)
            if row is not None
        ]
        if records and not rows:
            raise ContentUnavailableError(
                f"{len(records)} memory rows came back with no `content` field. "
                "Reflection verifies every quote against the memory it cites, "
                "which needs the text in the index. Enable content storage on "
                "the vector backend (ov-postgres: `store_content`; OpenViking: "
                "the adapter's USE_CONTENT_FIELD) and re-index."
            )
        return rows

    async def neighbours(self, row: MemoryRow, *, limit: int) -> list[MemoryRow]:
        """Return memories semantically near ``row``, excluding itself.

        Search is used to pick *which* memories, then their text is fetched
        through :meth:`rows`. A ``MatchedContext`` carries ``abstract`` but no
        ``content`` and no timestamps, so using it directly would hand the
        model a summary to quote and stamp every neighbour with the current
        time -- while the prompt asks it to prefer memories written apart.
        """
        result = await self._fs.search(
            query=row.text,
            target_uri=self._user_root,
            # Over-ask, because the evidence filter below discards results and a
            # neighbour dropped for being a preference should not cost a slot.
            limit=(limit + 1) * _EVIDENCE_OVERREAD,
            level=[2],
            ctx=self._ctx,
        )
        uris = [
            uri
            for uri in (self._stored_uri(context) for context in result.memories or [])
            if uri is not None and uri != row.uri and self._is_evidence(uri)
        ]
        if len(uris) < limit:
            logger.debug(
                "ov-ext reflect: %d of %d neighbours survived the evidence filter for %s",
                len(uris),
                limit,
                row.uri,
            )
        return self._as_context(await self.rows(uris[:limit]))

    @staticmethod
    def _stored_uri(context: Any) -> str | None:
        """The stored URI behind a retrieval result, or ``None`` to skip it.

        Retrieval appends a level suffix -- ``/.abstract.md`` for L0,
        ``/.overview.md`` for L1. These are level-2 results so no suffix is
        expected, but one would name a generated summary rather than a memory,
        and citing it is exactly what the design forbids.
        """
        uri = getattr(context, "uri", None)
        if not uri:
            return None
        uri = str(uri)
        if uri.endswith("/.abstract.md") or uri.endswith("/.overview.md"):
            return None
        return uri

    async def tail_sample(self, *, limit: int) -> list[MemoryRow]:
        """Return a few memories from the far end of the store.

        memex samples with ``ORDER BY random()``. OpenViking's filter API has
        no random ordering, so this reads a wider window of the least recently
        updated memories and picks from it at random. Taking the oldest *n*
        directly would return the same rows in every batch of every sweep --
        a constant, not a sample, and a constant cannot break an echo chamber.
        """
        from openviking.storage.expr import And, Eq, In, PathScope

        if limit <= 0:
            return []
        kinds = ["memory"]
        if "resources" in self._settings.evidence_types:
            kinds.append("resource")
        records = await self._db.filter(
            filter=And(
                [
                    PathScope("uri", self._user_root),
                    In("context_type", kinds),
                    Eq("level", 2),
                ]
            ),
            # Over-read for the same reason as the neighbour search: the rows
            # this returns are filtered by `_is_evidence` afterwards.
            limit=limit * _TAIL_WINDOW * _EVIDENCE_OVERREAD,
            output_fields=_ROW_FIELDS,
            order_by="updated_at",
            order_desc=False,
            ctx=self._ctx,
        )
        rows = [
            row
            for row in (self._row_from_record(record) for record in records)
            if row is not None and self._is_evidence(row.uri)
        ]
        if len(rows) <= limit:
            return self._as_context(rows)
        return self._as_context(self._rng.sample(rows, limit))

    def _as_context(self, rows: Sequence[MemoryRow]) -> list[MemoryRow]:
        """Trim rows shown only as background to ``context_chars``.

        A changed memory is delta-sized already; a neighbour is the whole file,
        and five of them undo the delta work on their own. Safe because quotes
        are verified against the row text the model was shown, so a quote can
        only come from the part that was sent.
        """
        cap = self._settings.context_chars
        if not cap:
            return list(rows)
        return [
            row
            if len(row.text) <= cap
            else MemoryRow(
                uri=row.uri,
                text=row.text[:cap],
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def read_overview(self, directory: str) -> str | None:
        """Return a directory's generated L1 overview, or ``None`` if absent."""
        if not directory:
            # A pooled delta batch spans directories; there is no single
            # overview that describes it.
            return None
        try:
            content = await self._fs.read_file(
                f"{directory.rstrip('/')}/.overview.md", ctx=self._ctx
            )
        except Exception:
            # A directory with no overview yet is the normal case, not a
            # failure: it is background for the prompt and the sweep runs
            # without it.
            return None
        return str(content) if content else None

    async def write_observation(self, observation: Observation) -> str:
        """Write an observation as a memory file and return its URI."""
        from openviking.session.memory.dataclass import MemoryFile
        from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

        topic = _topic(observation)
        name = f"{_subject(observation)}-{_digest(observation)}"
        root = self._settings.observations_root.replace(
            "viking://~", f"viking://user/{self._ctx.user.user_id}"
        )
        uri = f"{root.rstrip('/')}/{topic}/{name}.md"

        links = [
            {
                "from_uri": uri,
                "to_uri": source_uri,
                "link_type": "derived_from",
                "match_text": quote,
                "weight": 1.0 / len(observation.evidence),
                "description": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            for source_uri, quote in observation.evidence
        ]
        memory_file = MemoryFile(
            uri=uri,
            content=_render(observation),
            links=links,
            memory_type="observations",
            extra_fields={"topic": topic, "name": name},
        )
        self._warn_about_unrenderable(uri, observation)
        await self._fs.write_file(uri, MemoryFileUtils.write(memory_file), ctx=self._ctx)
        return uri

    def _warn_about_unrenderable(self, uri: str, observation: Observation) -> None:
        """Say when OpenViking will decline to link one of the quotes.

        ``render_links`` protects markdown spans, so a quote containing
        backticks overlaps a protected span and the link is dropped without a
        word. The bullet still names its source, so the provenance survives and
        only the click is lost -- but a silent drop is the kind of thing nobody
        notices, and this store's memories are about code.
        """
        try:
            from openviking.session.memory.utils.link_renderer import LinkRenderer

            body = _render(observation)
            for source_uri, quote in observation.evidence:
                if not LinkRenderer.can_render_link(body, quote, uri, source_uri):
                    logger.debug(
                        "ov-ext reflect: %s will not render a link for %r; the "
                        "bullet names its source but will not be clickable",
                        uri,
                        quote[:60],
                    )
        except Exception:
            # Diagnostics only. An upstream rename here must not cost a write.
            logger.debug("ov-ext reflect: could not check link rendering", exc_info=True)

    async def link(
        self,
        from_uri: str,
        to_uri: str,
        *,
        link_type: str,
        match_text: str | None = None,
        weight: float = 0.5,
    ) -> None:
        """Record a typed edge on an existing memory file.

        Reads, appends and writes back rather than replacing, so everything
        already in the file -- links included -- stays. That mattered more when
        reflection also wrote ``contradicts`` edges onto memories it did not
        author; since that pass was removed the only caller links an observation
        to its sources, and the file being rewritten is reflection's own. The
        merge stays anyway: it is what makes writing the same edge twice
        idempotent, which is what lets a re-run sweep be harmless.
        """
        from openviking.session.memory.merge_op.link_merge import merge_links
        from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

        raw = await self._fs.read_file(from_uri, ctx=self._ctx)
        memory_file = MemoryFileUtils.read(raw, uri=from_uri)
        link = {
            "from_uri": from_uri,
            "to_uri": to_uri,
            "link_type": link_type,
            "weight": weight,
            "description": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if match_text is not None:
            link["match_text"] = match_text
        # OpenViking's own merge: dedupes on (from, to, match_text) and keeps
        # the higher weight, so recording the same tension twice is idempotent.
        memory_file.links = merge_links(memory_file.links or [], [link])
        await self._fs.write_file(
            from_uri, MemoryFileUtils.write(memory_file), ctx=self._ctx
        )


def _topic(observation: Observation) -> str:
    """The folder an observation is filed under.

    The entity category it is about, when one of its sources is an entity --
    which is the usual case, since entities are what the sweep reflects on.

    Not the last segment of the first area, which is what this used to be. That
    reads well for ``memories/entities/dev_tool/x.md`` and absurdly for
    ``memories/events/2026/09/07/x.md``, where the last segment is the day of
    the month -- which is how a store ends up with folders called 06, 07 and 08.
    """
    for uri in sorted(observation.sources):
        parts = uri.split("/memories/", 1)
        if len(parts) != 2:
            continue
        segments = parts[1].split("/")
        if segments[0] == "entities" and len(segments) > 2:
            return _slug(segments[1])
    # No entity among the sources: the memory type itself, which is at least a
    # word rather than a number.
    for uri in sorted(observation.sources):
        parts = uri.split("/memories/", 1)
        if len(parts) == 2:
            return _slug(parts[1].split("/")[0])
    return "observations"


def _subject(observation: Observation) -> str:
    """A readable, *stable* name for what the observation is about.

    The memories it cites, not the title the model gave it. A title is the
    model's wording and it does not survive a re-run: the same claim from the
    same evidence came back as "Jasper consistently measures and optimizes
    token usage", "Jasper focuses on token usage" and "Token usage and
    measurement is a recurring concern" -- three files, one observation, and a
    digest that was doing its job while the title in the filename cancelled it.
    """
    names = []
    for uri in sorted(observation.sources):
        stem = uri.rsplit("/", 1)[-1].removesuffix(".md")
        slug = _slug(stem)
        if slug not in names:
            names.append(slug)
    return "_".join(names[:2]) or "observation"


def _digest(observation: Observation) -> str:
    """A short stable hash of what an observation rests on.

    Over the cited memories *and the spans cited in them*, both normalised.
    Sources alone would collapse two genuinely different readings of the same
    two memories into one file; adding the wording would stop a re-run
    colliding at all, which is the bug this replaced. The quotes are the
    middle: they are what the claim is built from, and the same claim drawn
    again from the same evidence quotes the same spans.

    Whitespace is normalised because the model reflows -- a quote differing
    only in a line break is the same quote, and the verifier already treats it
    that way.
    """
    material = "\n".join(
        sorted(f"{uri}\x00{normalise(quote)}" for uri, quote in observation.evidence)
    )
    return hashlib.sha256(material.encode()).hexdigest()[:8]


def _slug(text: str) -> str:
    """Reduce free text to a lowercase, underscore-joined path segment."""
    cleaned = "".join(char if char.isalnum() else " " for char in text.lower())
    words = cleaned.split()
    return "_".join(words[:5]) or "untitled"


def _label(uri: str) -> str:
    """Name a cited memory readably, and unambiguously.

    The path below the user root, without the extension. The file stem alone is
    not enough: an observation citing ``entities/browser_extension/ov_clip.md``
    and ``entities/software_package/ov_clip.md`` would name both ``ov_clip``,
    and a reader could not tell which quote came from which. Splitting below
    the user root rather than below ``memories/`` covers resources too, where
    every chunk of every article is called ``chunk_1``, ``chunk_2``...
    """
    marker = (
        f"/user/{uri.split('/user/', 1)[1].split('/', 1)[0]}/" if "/user/" in uri else ""
    )
    tail = uri.split(marker, 1)[1] if marker and marker in uri else uri.rsplit("/", 1)[-1]
    return tail.removesuffix(".md")


def _render(observation: Observation) -> str:
    """Format an observation as the Markdown body of its memory file.

    The shape matches what ``observations.yaml`` tells the extractor to expect:
    a title, the claim, then an Evidence section whose bullets are the verified
    quotes. Every quote here has already been found in the memory it cites.

    The source is named, not spelled out as a URI, and nothing here writes a
    markdown link. OpenViking's ``LinkRenderer`` makes one itself: each
    ``derived_from`` link carries the quote as its ``match_text``, and
    rendering finds that span in this body and turns it into the link. Writing
    an explicit ``[uri](uri)`` as well produced two links per bullet -- the
    whole URI as its own label, and the quote linkified inside its quotation
    marks.
    """
    lines = [f"# {observation.title}", "", observation.content, "", "## Evidence", ""]
    lines += [f'- {_label(uri)}: "{quote}"' for uri, quote in observation.evidence]
    return "\n".join(lines)
