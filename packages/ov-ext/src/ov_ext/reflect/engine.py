"""The sweep: gather, propose, contradict, verify, write.

memex runs seven numbered phases against a ``mental_models`` table, with
compare-and-swap writes, a Postgres work queue and dead-lettering. Almost all
of that exists to make a row update safe under concurrency. OpenViking has no
such row -- an observation is a memory file behind a URI -- so what is left is
one function:

1. ask the store what changed since the watermark;
2. group by directory, so one batch is one area of the store;
3. gather evidence: the changed memories, their semantic neighbours, and a few
   at random;
4. one model call for observations, one for contradictions;
5. verify every quote in code;
6. write what survived, and move the watermark.

The two model calls share a single gathered batch and a single citation map,
so contradiction detection costs one extra call rather than a second pipeline.

Nothing here raises on a bad batch. A directory whose model call fails, or
whose observations all fail verification, is counted and skipped -- a sweep
covering ten areas should not lose nine of them to one bad response.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..observability import annotate, record_error, traced
from .citations import build_memory_context, citation_map
from .config import ReflectSettings
from .models import (
    Contradictions,
    MemoryRow,
    Observation,
    ProposedObservations,
    ReflectMemoryContext,
)
from .ports import MemoryStore, StructuredLLM
from .prompts import contradiction_prompt, propose_prompt
from .verify import verify_observations
from .watermark import Watermark

__all__ = ["ReflectionEngine", "SweepReport"]

logger = logging.getLogger(__name__)

# Span and attribute prefix for this subsystem, matching `ov_ext.retrieval`.
NAMESPACE = "ov_ext.reflect"

# Relations worth recording. "reinforce" is dropped deliberately: agreement is
# the normal state of a memory store, so linking it would add an edge to most
# pairs and drown the disagreements that matter.
_RECORDED_RELATIONS = {"contradict": 0.9, "weaken": 0.5}


@dataclass
class SweepReport:
    """What one sweep did, for the caller and the logs.

    Attributes
    ----------
    batches : int
        Directories examined.
    proposed : int
        Observations the model offered, before verification.
    written : int
        Observations that survived and were written.
    contradictions : int
        Links recorded between memories in tension.
    dropped : dict[str, int]
        Why observations were discarded, keyed by reason. The ratio of this to
        ``proposed`` is the signal that a prompt change made things worse.
    failures : int
        Batches abandoned because a model call failed or returned nothing.
    """

    batches: int = 0
    proposed: int = 0
    written: int = 0
    contradictions: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    failures: int = 0

    def record_drops(self, counts: dict[str, int]) -> None:
        """Fold one batch's drop counts into the running totals."""
        for reason, count in counts.items():
            self.dropped[reason] = self.dropped.get(reason, 0) + count


def group_by_directory(uris: Sequence[str]) -> dict[str, list[str]]:
    """Group stored URIs by their parent directory, preserving order.

    One batch per directory rather than one big batch, because a directory is
    the unit OpenViking already summarizes: it has an overview to use as
    background, and its members are more likely to bear on each other than two
    memories drawn from opposite ends of the store.
    """
    grouped: dict[str, list[str]] = {}
    for uri in uris:
        parent = uri.rsplit("/", 1)[0] if "/" in uri else uri
        grouped.setdefault(parent, []).append(uri)
    return grouped


class ReflectionEngine:
    """Runs reflection sweeps against a store.

    Parameters
    ----------
    store :
        Where memories are read and observations written.
    llm :
        The model, answering in a given shape.
    settings :
        Behaviour toggles. Read from the environment when omitted.
    """

    def __init__(
        self,
        store: MemoryStore,
        llm: StructuredLLM,
        settings: ReflectSettings | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._settings = settings or ReflectSettings()

    @traced("ov_ext.reflect.sweep")
    async def sweep(self, watermark: Watermark, *, now: datetime | None = None) -> tuple[
        SweepReport, Watermark
    ]:
        """Reflect on everything changed since ``watermark``.

        Parameters
        ----------
        watermark :
            How far the last sweep read.
        now :
            Treated as the current time when stamping the new watermark.
            Passed in so the caller owns the clock and tests stay
            deterministic; defaults to now in UTC.

        Returns
        -------
        tuple[SweepReport, Watermark]
            What happened, and the mark to store. The returned watermark is
            unchanged when nothing was read, so a caller can persist it
            unconditionally.
        """
        settings = self._settings
        moment = now or datetime.now(timezone.utc)
        report = SweepReport()

        changed = await self._store.changed_since(
            watermark.last_seen, limit=settings.batch_limit
        )
        annotate({"ov_ext.reflect.changed": len(changed)})
        if not changed:
            annotate({"ov_ext.reflect.outcome": "nothing_changed"})
            return report, watermark

        newest = watermark.last_seen
        for directory, uris in group_by_directory(changed).items():
            batch_newest = await self._run_batch(directory, uris, report)
            if batch_newest is not None:
                newest = max(newest, batch_newest)

        annotate(
            {
                "ov_ext.reflect.batches": report.batches,
                "ov_ext.reflect.proposed": report.proposed,
                "ov_ext.reflect.written": report.written,
                "ov_ext.reflect.contradictions": report.contradictions,
                "ov_ext.reflect.failures": report.failures,
                "ov_ext.reflect.outcome": "ok",
            }
        )
        return report, watermark.advanced_to(newest, now=moment)

    @traced("ov_ext.reflect.batch")
    async def _run_batch(
        self, directory: str, uris: Sequence[str], report: SweepReport
    ) -> datetime | None:
        """Reflect on one directory's changed memories.

        Returns the newest ``created_at`` among the changed rows actually read,
        so the caller can advance the watermark to what was seen rather than to
        the wall clock. ``None`` when the batch produced nothing to advance on.
        """
        report.batches += 1
        annotate({"ov_ext.reflect.directory": directory, "ov_ext.reflect.changed": len(uris)})

        changed_rows = await self._store.rows(uris)
        if not changed_rows:
            annotate({"ov_ext.reflect.outcome": "no_rows"})
            return None

        gathered = await self._gather(changed_rows)
        rows_by_uri = {row.uri: row for row in gathered}
        uri_to_index, index_to_uri = citation_map([row.uri for row in gathered])
        contexts = build_memory_context(gathered, uri_to_index)
        annotate({"ov_ext.reflect.gathered": len(gathered)})

        scope = await self._store.read_overview(directory)
        await self._propose(contexts, scope, index_to_uri, rows_by_uri, report)

        if self._settings.contradictions:
            await self._contradict(contexts, index_to_uri, report)

        return max(row.created_at for row in changed_rows)

    async def _gather(self, changed: Sequence[MemoryRow]) -> list[MemoryRow]:
        """Collect the evidence pool: what changed, its neighbours, and a tail.

        Deduplicated by URI, with the changed rows first so they get the low
        citation indices -- the model reads them as the subject and the rest as
        context.
        """
        settings = self._settings
        pool: dict[str, MemoryRow] = {row.uri: row for row in changed}

        if settings.neighbour_limit:
            for row in changed:
                for neighbour in await self._store.neighbours(
                    row, limit=settings.neighbour_limit
                ):
                    pool.setdefault(neighbour.uri, neighbour)

        if settings.tail_sample:
            for row in await self._store.tail_sample(limit=settings.tail_sample):
                pool.setdefault(row.uri, row)

        return list(pool.values())

    async def _propose(
        self,
        contexts: Sequence[ReflectMemoryContext],
        scope: str | None,
        index_to_uri: dict[int, str],
        rows_by_uri: dict[str, MemoryRow],
        report: SweepReport,
    ) -> None:
        """Ask for observations, verify them, and write the survivors."""
        settings = self._settings
        prompt = propose_prompt(contexts, scope=scope)

        try:
            proposed = await self._llm.complete(prompt, ProposedObservations)
        except Exception as exc:  # the model is a network call; a batch may fail
            record_error(exc, "propose_failed", NAMESPACE)
            report.failures += 1
            return

        if proposed is None:
            annotate({"ov_ext.reflect.outcome": "propose_unparsed"})
            report.failures += 1
            return

        report.proposed += len(proposed.observations)
        kept, dropped = verify_observations(
            proposed.observations,
            index_to_uri,
            rows_by_uri,
            min_evidence=settings.min_evidence,
            require_cross_peer=settings.require_cross_peer,
        )
        report.record_drops(dropped)

        for observation in kept:
            await self._write(observation, report)

    async def _write(self, observation: Observation, report: SweepReport) -> None:
        """Persist one observation and link it to every memory it cites."""
        if self._settings.dry_run:
            logger.info(
                "ov-ext reflect (dry run): would write %r citing %d memories",
                observation.title,
                len(observation.evidence),
            )
            report.written += 1
            return

        uri = await self._store.write_observation(observation)
        for source_uri, quote in observation.evidence:
            # `derived_from` is OpenViking's own label for a summary drawn from
            # other memories, and `match_text` is already contracted to appear
            # verbatim -- which is exactly what verification just proved.
            await self._store.link(
                uri,
                source_uri,
                link_type="derived_from",
                match_text=quote,
                weight=1.0 / len(observation.evidence),
            )
        report.written += 1

    async def _contradict(
        self,
        contexts: Sequence[ReflectMemoryContext],
        index_to_uri: dict[int, str],
        report: SweepReport,
    ) -> None:
        """Ask which memories are in tension and record an edge for each pair."""
        try:
            found = await self._llm.complete(
                contradiction_prompt(contexts),
                Contradictions,
            )
        except Exception as exc:
            record_error(exc, "contradict_failed", NAMESPACE)
            report.failures += 1
            return

        if found is None:
            annotate({"ov_ext.reflect.outcome": "contradict_unparsed"})
            report.failures += 1
            return

        for relationship in found.relationships:
            weight = _RECORDED_RELATIONS.get(relationship.relation.strip().lower())
            if weight is None:
                continue
            left = index_to_uri.get(relationship.left_index)
            right = index_to_uri.get(relationship.right_index)
            if left is None or right is None or left == right:
                continue
            if self._settings.dry_run:
                logger.info(
                    "ov-ext reflect (dry run): would link %s contradicts %s", left, right
                )
            else:
                # No match_text: the claim is about two memories taken as
                # wholes, and OpenViking checks match_text verbatim -- inventing
                # a span here would fail that check or, worse, pass it by
                # accident.
                await self._store.link(
                    left, right, link_type="contradicts", weight=weight
                )
            report.contradictions += 1
