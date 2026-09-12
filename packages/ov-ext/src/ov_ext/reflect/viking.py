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
point. OpenViking's own ``MemoryFileUtils`` serializes the file, so the format
is theirs rather than ours.

A direct write is *not* on its own a first-class memory, which this module
claimed for months and which was false. ``write_file`` writes bytes: the vector
row, the ``abstract`` shown in the UI and the directory overview are all built
during vectorization, which runs from ``apply_operations`` or the content-write
path and never from a raw write. Every observation written before this was on
disk and absent from search. :meth:`VikingStore._index` is the step that was
missing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections import Counter
from collections.abc import Collection, Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel

from .citations import parse_timestamp
from .config import ReflectSettings
from .exceptions import ContentUnavailableError, ObservationUnreadableError
from .models import MemoryRow, Observation
from .verify import areas_of, is_resource

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

# How much wider than the requested batch to read before filtering by memory
# type. Most deltas in a busy store are entities, so a small factor is enough.
_DELTA_OVERREAD = 4

# How much wider than needed to read before the evidence filter runs. A search
# over the whole user root returns preferences and peer memories too, and one
# discarded should not cost a neighbour its slot.
_EVIDENCE_OVERREAD = 3

# Between two changes to one memory in a batch. Verification normalises
# whitespace before comparing, so joining with a newline would let a quote run
# from the end of one change into the start of another -- a span that exists in
# no memory, verified against a row that only looks like one. The rule is a
# token normalisation cannot collapse away.
_DELTA_SEPARATOR = "\n\n---\n\n"

# Heading the rendered evidence bullets sit under. Written by `_render` and
# looked for by `_claim_of`, which is why it is one constant and not two
# strings that have to be kept the same.
_EVIDENCE_HEADING = "## Evidence"

# Longest observation filename before it is shortened and given a digest. Well
# inside the 255 bytes a filesystem allows, with room for the `.md` and for a
# store that is later exported onto a path with a deep prefix.
_MAX_NAME_CHARS = 80


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

        from openviking.storage.expr import And, Eq, Or, PathScope, TimeRange

        types = list(self._settings.memory_types)
        if not types:
            # Nothing is reflectable, so there is nothing to ask for. Guarded
            # because an empty `Or` is an empty disjunction, which a backend is
            # as likely to compile to "everything" as to "nothing".
            return []

        # level=2 is the memory itself. Levels 0 and 1 are the generated
        # directory abstract and overview, whose refresh deliberately lags
        # their contents -- using them as a change signal would miss changes in
        # exactly the large directories that matter most.
        condition = And(
            [
                # Scoped to the reflectable types, not to the memory root with a
                # filter afterwards. The root holds every type OpenViking ships
                # -- events, preferences, trajectories, cases, and reflection's
                # own observations -- while only `memory_types` is reflected on,
                # so filtering in Python made `limit` mean "rows to read" rather
                # than "memories to reflect on". A busy stretch of events then
                # filled the window with rows that were all discarded, and the
                # sweep reported nothing changed while holding the watermark:
                # the same rows, every tick, forever. Asking the index for the
                # right subtrees means the limit counts what it says it counts.
                Or([PathScope("uri", f"{self._memory_root}/{name}") for name in types]),
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
        records = list(records)
        changed = [
            str(record["uri"])
            for record in records
            if record.get("uri") and parse_timestamp(record.get("updated_at")) > moment
        ]
        if records and not changed and len(records) >= limit:
            # Every row came back sitting exactly on the mark, and the page was
            # full. `TimeRange` compiles `start` to `>=`, so those rows are read
            # and dropped every sweep, and anything newer is behind them --
            # the sweep reports "nothing changed" and holds the watermark
            # forever. Rare, because it needs `batch_limit` memories written
            # within one timestamp tick, which is a bulk import rather than a
            # day's work. Said loudly rather than fixed by nudging the mark
            # past them: that would skip memories nothing has reflected on,
            # which is the failure this whole path is arranged to avoid.
            logger.error(
                "ov-ext reflect: all %d rows read sit exactly on the watermark "
                "(%s) and the page is full, so the sweep cannot see past them "
                "and will not advance. Raise OV_REFLECT_BATCH_LIMIT above the "
                "number of memories sharing that timestamp.",
                len(records),
                moment.isoformat(),
            )
        return changed

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
        if skipped and not self._settings.dry_run:
            # Not during a dry run. This is the second place deltas are retired
            # -- the engine's `mark_reflected` after a batch is the other -- and
            # guarding only that one still let a dry run write to the delta
            # table. The case it ruins is the main reason to dry-run at all:
            # previewing a change to `memory_types`. Run the preview on the old
            # setting and every delta the new setting would have covered is
            # retired as "not reflectable", so the real run finds nothing left.
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

    def observation_uri(
        self, observation: Observation, subjects: Collection[str] = ()
    ) -> str:
        """Where an observation belongs: one file per memory reflected on.

        Pure, and separate from the write, because the engine has to know the
        file *before* it writes -- that is where it looks for the observation
        already standing there and asks the model what holds now.

        Parameters
        ----------
        observation :
            The verified observation to file.
        subjects :
            The memories this batch was reflecting on. What an observation is
            filed under, when it cites one of them -- see :func:`_primary` for
            why the evidence alone is not a stable enough answer.
        """
        return self._uri_for(observation, list(subjects))

    async def resolve(
        self, observation: Observation, subjects: Collection[str] = ()
    ) -> tuple[str, Observation | None]:
        """The file an observation belongs in, and what already stands there.

        One call rather than two, because the engine must not write before it
        knows what it would be writing over.

        The file is the subject's, and only the subject's. Reusing a *different*
        subject's file when the subject has none of its own was tried, to close
        one real gap: an observation about two entities is filed under whichever
        of them changed, so as first one and then the other is edited, the same
        running claim alternates between two names. Two files, neither
        superseding the other.

        It does not survive contact with a real store. Any structural test for
        "these two are the same claim" has to be computed over the files'
        citations, and citations accumulate: every revision folds new sources
        into the standing observation, so the most-revised file ends up with the
        widest basin, and the busiest memory in a personal store -- the user's
        own person entity -- pulls in the first observation about every new
        entity. That entity's own file is then never created, so every later
        observation about it is pulled in too, and the revise pass is handed
        unrelated claims with instructions to merge them. Two successive
        versions of that test each looked airtight and each regrew the same
        magnet one hop further out.

        So the alternation stands, and is the documented cost. It is bounded --
        one file per entity the claim is co-cited with and that is itself
        reflected on, each of them evolving in place -- while the merge it was
        traded against is unbounded and cannot be undone. Wider than "a pair":
        three entities taking turns give three files, and a claim whose cited
        partners drift gives one per partner. The duplication is of the claim,
        not of the file: each file is legitimately what has been observed about
        its own entity. Closing it properly needs a test of whether two claims
        *say* the same thing, which is a model call and a separate feature.

        Parameters
        ----------
        observation :
            The verified observation to file.
        subjects :
            The memories this batch reflected on.

        Returns
        -------
        tuple[str, Observation | None]
            Where to write, and the observation already there, if any.

        Raises
        ------
        ObservationUnreadableError
            When the file could not be read, so it is unknown whether an
            observation is already there.
        """
        uri = self.observation_uri(observation, subjects)
        return uri, await self.read_observation(uri)

    def _uri_for(self, observation: Observation, ranked: Sequence[str]) -> str:
        """The URI an observation takes when ``ranked`` names its subject."""
        root = self._settings.observations_root.replace(
            "viking://~", f"viking://user/{self._ctx.user.user_id}"
        )
        # Filed under this one candidate: passing it as the batch's subjects
        # makes it outrank everything else in the evidence, which is what
        # "what would this be called if it were about that memory" means.
        return f"{root.rstrip('/')}/{_topic(observation, ranked)}/{_subject(observation, ranked)}.md"

    async def whole_memories(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Return the memories behind ``uris`` in full, never as deltas.

        :meth:`rows` deliberately serves a changed memory as the lines that
        changed, which is the whole point of the delta path. That makes it the
        wrong thing to re-check a carried-forward quote against: the quote was
        drawn from the whole memory, and comparing it to this sweep's few edited
        lines reports every older citation as stale and deletes it. Since the
        subject of a revision is by construction a memory that just changed,
        that would strip an observation's provenance on every pass.
        """
        return await self._rows_from_index(uris)

    async def read_observation(self, uri: str) -> Observation | None:
        """Return the observation already written at ``uri``, or ``None``.

        Rebuilt from the file's own ``derived_from`` links rather than by
        parsing the Evidence bullets back out of the prose. The links are what
        this store wrote, one per verified quote with the quote as
        ``match_text``, so the round trip carries exactly the evidence that was
        checked -- while a bullet is rendered text, and re-reading it would
        promote whatever the file happens to say into a citation.

        Returns ``None`` when there is no file yet, which is the usual case: an
        entity gets an observation the first time a sweep has something to say
        about it.

        Raises
        ------
        ObservationUnreadableError
            When the read failed for any reason other than the file not being
            there, so what is at ``uri`` is unknown rather than absent.
        """
        from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

        from openviking_cli.exceptions import NotFoundError

        try:
            raw = await self._fs.read_file(uri, ctx=self._ctx)
        except NotFoundError:
            # No file yet: the usual case, and the only one that means "write a
            # first observation here".
            return None
        except Exception as exc:
            # Anything else -- unreachable, timed out, not permitted -- means
            # nothing is known about what is there. Raised rather than reported
            # as absence, because the caller's response to absence is to write,
            # and writing over a file you could not read destroys every revision
            # it accumulated. A sweep that skips one observation during an
            # outage costs a sweep; this costs the memory.
            raise ObservationUnreadableError(
                f"could not read the observation standing at {uri}"
            ) from exc
        if not raw:
            return None
        try:
            memory_file = MemoryFileUtils.read(raw, uri=uri)
        except Exception as exc:
            raise ObservationUnreadableError(
                f"{uri} did not parse as a memory file"
            ) from exc

        evidence = tuple(
            (str(link["to_uri"]), str(link["match_text"]))
            for link in (memory_file.links or [])
            if link.get("link_type") == "derived_from"
            and link.get("to_uri")
            and link.get("match_text")
        )
        if not evidence:
            # Parsing does not fail on text that is not a memory file --
            # `MemoryFileUtils.read` hands back the raw bytes as `content` --
            # so "it parsed" is not evidence that this is an observation. Every
            # one this store writes carries a `derived_from` link per quote, so
            # none at all means either something else is at this URI or an
            # observation has lost its provenance. Refused either way: the
            # caller writes on `None`, and writing here would replace whatever
            # is there with a file built from text nobody verified.
            raise ObservationUnreadableError(
                f"{uri} carries no derived_from links, so it is not an "
                "observation this sweep can revise"
            )
        # `plain_content`, not `content`. OpenViking linkifies each link's
        # `match_text` where it appears in the body, and a claim usually
        # restates what it cites -- so the stored H1 and paragraph come back
        # carrying `[quote](viking://...)`. Read raw, that markup becomes the
        # model's input and then the file's canonical claim, compounding every
        # revision. Stripping puts back what was written.
        body = str(memory_file.plain_content() or "")
        return Observation(
            title=_title_of(body),
            content=_claim_of(body),
            evidence=evidence,
            areas=areas_of(uri for uri, _ in evidence),
        )

    async def write_observation(
        self, observation: Observation, *, uri: str | None = None
    ) -> str:
        """Write an observation as a memory file and return its URI.

        Parameters
        ----------
        observation :
            What to write. Its own :meth:`observation_uri` is used when no
            ``uri`` is given.
        uri :
            Write here instead. The engine passes the file it read the standing
            observation from, because folding in new evidence can change which
            memory is quoted most -- and a revision that relocated itself would
            leave the file it was revising untouched and start a second one,
            which is the sprawl this naming exists to end.
        """
        from openviking.session.memory.dataclass import MemoryFile
        from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

        if not observation.evidence:
            # Every line of an observation is supposed to be traceable to a
            # `derived_from` link, so one with no evidence is not an observation
            # this store writes. Refused rather than written with no links,
            # which would also divide by zero on the weight below.
            raise ValueError(
                f"refusing to write {uri or 'an observation'} with no evidence"
            )
        uri = uri or self.observation_uri(observation)
        topic, name = uri.rsplit("/", 2)[-2:]
        name = name.removesuffix(".md")

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
        await self._index(uri)
        return uri

    async def _index(self, uri: str) -> None:
        """Put a written observation into the index, and refresh its folder.

        ``write_file`` writes bytes and stops. Everything that makes a memory
        findable -- the vector row, the ``abstract`` the UI shows, the entry in
        the directory overview -- is done by ``MemoryUpdater`` during
        vectorization, and that runs from ``apply_operations`` or from the
        content-write path, neither of which a raw write touches. So an
        observation written here existed on disk and nowhere else: no abstract,
        and absent from search entirely. Measured against the live store, a
        semantic search scoped to the observations tree returned nothing while
        the same search over entities returned five hits.

        Added after the write rather than by moving to the content-write
        coordinator. That coordinator is the blessed path and does slightly
        more, but it also takes a path lock and raises where this does not, and
        the write itself is not what was broken. This is the step that was
        missing; a failure here leaves exactly what was there before.

        The abstract is not model-written, incidentally -- it is the
        observation's own text with links stripped, truncated. There is no
        second model call here.

        Runs before the engine's ``link`` calls, which re-serialize the file and
        grow it -- ``MemoryFileUtils.write`` renders links back into the body.
        The index stays correct anyway because the abstract is computed through
        ``strip_all_links``, so the linkified and plain bodies reduce to the
        same text. That is an upstream detail rather than a guarantee: if the
        rendering or the stripping changes, this needs re-checking.
        """
        from openviking.session.memory.memory_updater import MemoryUpdater

        try:
            indexed = await MemoryUpdater.refresh_file_embedding(
                viking_fs=self._fs,
                vikingdb=self._db,
                uri=uri,
                memory_type="observations",
                ctx=self._ctx,
            )
            if not indexed:
                # False covers three cases upstream -- no embedding queue, no
                # rows attempted, and any internal exception, which it logs
                # itself and swallows. So the cause is not ours to name; the
                # consequence is, and that is the part an operator needs.
                logger.warning(
                    "ov-ext reflect: %s was written but not indexed, so it will "
                    "not come back from search. Check the vector backend's "
                    "embedding queue, and openviking.session.memory."
                    "memory_updater at WARNING for the reason.",
                    uri,
                )
            # The directory holding the file, NOT the observations root.
            # `generate_overview` lists a directory's direct `.md` children, and
            # the root's direct children are topic folders -- so pointing it
            # there finds no files and takes the *delete* branch, removing the
            # root's own overview on every single write. Worse, that branch
            # guards a recursive remove with `all(...)` over the listing, and
            # `all([])` is True: an empty listing at the root would read as
            # "this directory is empty, delete it".
            await MemoryUpdater.refresh_schema_overview(
                viking_fs=self._fs,
                directory_uri=uri.rsplit("/", 1)[0],
                ctx=self._ctx,
            )
        except Exception:
            # The observation is already on disk. Failing the sweep over the
            # indexing step would lose the write as well as the index.
            logger.exception("ov-ext reflect: could not index %s", uri)

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


def _ranked(observation: Observation, subjects: Collection[str] = ()) -> list[str]:
    """The memories an observation could be filed under, best first.

    More than one, because the best answer is not always available. See
    :func:`_primary` for the ordering and :meth:`VikingStore.resolve` for what
    the runners-up are for.
    """
    counts = Counter(uri for uri, _ in observation.evidence)
    changed = set(subjects)
    return sorted(
        counts,
        key=lambda uri: (
            0 if uri in changed else 1,
            _rank(uri),
            -counts[uri],
            _stem(uri),
            uri,
        ),
    )


def _primary(observation: Observation, subjects: Collection[str] = ()) -> str:
    """The memory an observation is about, and is therefore filed under.

    ``subjects`` is what the sweep was reflecting *on* -- the memories that
    changed in this batch. It decides, and everything else is a tiebreak,
    because the changed memory is the only thing in an observation that does not
    vary with the draw. Evidence does: a batch pairs the changed memory with
    whichever neighbours the sample happened to surface, so ranking on evidence
    alone files the same running claim under a different partner every sweep.
    Measured against the live store, five observations about ``blog_scraper``
    landed in ``dev_tool/embark``, ``cloud_service/azure``, ``person/jasper``
    and ``library/httpx`` -- four of them named after the other entity, because
    with two sources quoted once each the alphabetical tiebreak fell through to
    the category folder at the front of the URI.

    Worse than untidy: unrelated claims sharing one busy partner -- and in a
    personal store the busiest is the user's own ``person`` entity -- were then
    handed to the revise pass together, which merges what it is given. Sprawl is
    recoverable. A model folding "Zed is the editor of choice" into "the scraper
    retries with backoff" is not.

    Within a tier: most quotes, then the memory's own name, then its full URI.
    The name before the URI so a tiebreak turns on what the memory is called
    rather than on which folder it happens to sit in.

    This is the *best* answer, not the only acceptable one. An observation about
    a pair of entities is filed under whichever of them changed, so the same
    running claim alternates between two files as first one and then the other
    is edited. :meth:`VikingStore.resolve` closes that by preferring a file that
    already exists over a better-ranked one that does not.
    """
    ranked = _ranked(observation, subjects)
    return ranked[0] if ranked else ""


def _rank(uri: str) -> int:
    """How eligible a cited memory is to be what an observation is filed under.

    An entity first: entities are what the sweep reflects on and what an
    observation is *about*, while an event or a resource is how it was noticed.
    A resource last -- it is captured third-party text, so filing a finding
    about the user under someone else's article is wrong even when that article
    is where most of the quotes came from.
    """
    if "/memories/entities/" in uri:
        return 0
    return 2 if is_resource(uri) else 1


def _stem(uri: str) -> str:
    """The memory's own filename, without the extension."""
    return uri.rsplit("/", 1)[-1].removesuffix(".md")


def _topic(observation: Observation, subjects: Collection[str] = ()) -> str:
    """The folder an observation is filed under.

    The category of the memory it is about: ``software_project`` for an
    observation about ``entities/software_project/blog_scraper.md``, so the
    observations tree mirrors the entities tree it comments on.

    Not the last segment of the area, which is what this used to be. That reads
    well for ``memories/entities/dev_tool/x.md`` and absurdly for
    ``memories/events/2026/09/07/x.md``, where the last segment is the day of
    the month -- which is how a store ends up with folders called 06, 07 and 08.
    """
    primary = _primary(observation, subjects)
    parts = primary.split("/memories/", 1)
    if len(parts) == 2:
        segments = parts[1].split("/")
        if segments[0] == "entities" and len(segments) > 2:
            return _slug(segments[1])
        # Not an entity: the memory type itself, which is at least a word
        # rather than a number.
        return _slug(segments[0])
    if is_resource(primary):
        return "resources"
    return "observations"


def _subject(observation: Observation, subjects: Collection[str] = ()) -> str:
    """A readable, *stable* name for what the observation is about.

    The memory it is most about, not the title the model gave it and not the
    evidence it happens to rest on this time. Both of those were tried and both
    multiply files:

    A title is the model's wording and does not survive a re-run -- the same
    claim from the same evidence came back as "Jasper consistently measures and
    optimizes token usage", "Jasper focuses on token usage" and "Token usage and
    measurement is a recurring concern".

    A hash of the cited quotes fares no better. Each sweep samples different
    neighbours and the model quotes different spans, so the hash moves even when
    the claim does not: thirteen files in the live store were one running
    observation about ``blog_scraper``, each a snapshot nothing would ever
    revisit. Naming the subject instead means the next sweep opens the file that
    is already there.

    The file stem alone is not enough below a memory type that nests. Every
    daily reflection is called ``daily_reflection.md``, so an observation about
    2026-09-07 and one about 2026-09-08 would share a file and be merged into
    each other. ``_topic`` already spends the category on entities, so what is
    left of the path is folded into the name for everything else -- the same
    lesson ``_label`` learned, applied to the filename instead of the bullet.
    """
    primary = _primary(observation, subjects)
    if not primary:
        return "observation"
    parts = primary.split("/memories/", 1)
    if len(parts) != 2:
        return _slug(_stem(primary))
    segments = parts[1].removesuffix(".md").split("/")
    if segments[0] == "entities" and len(segments) > 2:
        # `entities/<category>/<name>`: the category is the folder, so the name
        # alone already says which memory this is.
        return _name(segments[-1])
    # Everything else: the memory type is the folder, so the rest of the path
    # has to carry the distinction. `2026/09/07/daily_reflection` becomes
    # `2026_09_07_daily_reflection`.
    return _name(" ".join(segments[1:] or segments))


def _slug(text: str, *, words: int | None = 5) -> str:
    """Reduce free text to a lowercase, underscore-joined path segment.

    ``words`` caps how many are kept, for a folder name that has to stay
    readable. ``None`` keeps all of them, for a name that has to stay *unique*
    -- see :func:`_name`.
    """
    cleaned = "".join(char if char.isalnum() else " " for char in text.lower())
    parts = cleaned.split()
    return "_".join(parts if words is None else parts[:words]) or "untitled"


def _name(text: str) -> str:
    """Name a memory's observation file, uniquely.

    A word cap cannot be used here. OpenViking's own entity names run long and
    differ at the end -- ``openviking_memory_plugin_for_claude_code`` and
    ``..._for_claude_desktop``, ``ov_clip_browser_extension_for_chrome`` and
    ``..._for_firefox`` -- so five words maps both members of each pair onto one
    file. Two distinct entities sharing a file is not untidy, it is destructive:
    the revise pass is then told they are one subject and asked to fold their
    claims into a single paragraph, and there is no other copy to recover from.

    So the whole name is kept, and only a name too long for a filesystem is
    shortened -- with a digest of the full name appended, so two that shared a
    prefix still land apart. A digest over the *entity's name* is stable in a
    way the evidence digest this replaced was not: the name is the same every
    sweep, while the quotes were never the same twice.
    """
    full = _slug(text, words=None)
    if len(full) <= _MAX_NAME_CHARS:
        return full
    keep = full[:_MAX_NAME_CHARS].rstrip("_")
    return f"{keep}_{hashlib.sha256(full.encode()).hexdigest()[:8]}"


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

    The title is flattened to one line. It is written as an H1 and read back as
    one, so a model that returned two lines would otherwise donate the second to
    the claim on the next revision, one line per revision.
    """
    title = " ".join(observation.title.split()) or "Observation"
    lines = [f"# {title}", "", observation.content, "", _EVIDENCE_HEADING, ""]
    lines += [f'- {_label(uri)}: "{quote}"' for uri, quote in observation.evidence]
    return "\n".join(lines)


def _title_of(body: str) -> str:
    """The title of a rendered observation: its H1, or its first line.

    The fallback matters because this parses a file the *previous* version of
    this module wrote, and a revision that lost the title would hand the model
    an untitled standing observation and get back a new name for something that
    already had one.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # `lstrip` rather than a fixed offset: a bare `#` is what an empty
            # title renders as, and slicing two characters off it yields the
            # hash itself as the observation's name.
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def _claim_of(body: str) -> str:
    """The claim of a rendered observation: everything between H1 and Evidence.

    The bullets are dropped rather than carried into the revise prompt. They
    are the quotes rendered for a reader, and feeding them back as prose is how
    a model comes to treat "the memory says X" as the observation instead of as
    its support.

    Split on the *last* Evidence heading, not the first. ``_render`` writes
    exactly one and writes it after the claim, so the last is always the real
    one -- while a claim that happens to contain that line, which a model
    writing about this very feature will produce, would be truncated at itself
    and the loss written back as the new claim.
    """
    lines = body.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.strip().startswith("#"):
            start = index + 1
            break
    end = len(lines)
    for index in range(len(lines) - 1, start - 1, -1):
        if lines[index].strip() == _EVIDENCE_HEADING:
            end = index
            break
    return "\n".join(lines[start:end]).strip()
