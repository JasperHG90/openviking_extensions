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

__all__ = ["BatchOutcome", "ReflectionEngine", "SweepReport"]

logger = logging.getLogger(__name__)

# Span and attribute prefix for this subsystem, matching `ov_ext.retrieval`.
NAMESPACE = "ov_ext.reflect"

# Relations worth recording. "reinforce" is dropped deliberately: agreement is
# the normal state of a memory store, so linking it would add an edge to most
# pairs and drown the disagreements that matter.
_RECORDED_RELATIONS = {"contradict": 0.9, "weaken": 0.5}


@dataclass(frozen=True)
class BatchOutcome:
    """What one directory's batch left behind for the watermark.

    Attributes
    ----------
    complete : bool
        Whether every model call in the batch finished. A batch that did not
        becomes a barrier the mark may not pass.
    newest : datetime | None
        Newest ``updated_at`` read. Set whether or not the batch completed:
        a completed batch advances the mark to it, and a failing one that has
        exhausted its stalls is stepped over to it.
    oldest : datetime | None
        Oldest ``updated_at`` read, which is where the barrier sits when the
        batch did not complete.
    """

    complete: bool
    newest: datetime | None
    oldest: datetime | None


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
    stepped_over : bool
        Whether the sweep forced the watermark past a batch that has been
        failing for more sweeps than ``max_stalls``. The memories in it were
        not reflected on.
    """

    batches: int = 0
    proposed: int = 0
    written: int = 0
    contradictions: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    failures: int = 0
    stepped_over: bool = False

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
    async def sweep(
        self, watermark: Watermark, *, now: datetime | None = None
    ) -> tuple[SweepReport, Watermark]:
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

        if not settings.enabled:
            # The last guard between "off by default" and unattended writes.
            # Checked here rather than only at registration, because the sweep
            # is reachable from a cron and a CLI that never called install().
            annotate({"ov_ext.reflect.outcome": "disabled"})
            return report, watermark

        changed = await self._store.changed_since(
            watermark.last_seen, limit=settings.batch_limit
        )
        annotate({"ov_ext.reflect.changed": len(changed)})
        if not changed:
            annotate({"ov_ext.reflect.outcome": "nothing_changed"})
            return report, watermark

        # A failed batch is a barrier, not merely a skipped one. Batches are
        # grouped by directory, so they are not in time order: without this, a
        # later directory holding newer rows would drag the mark past an
        # earlier directory that failed, and those memories would never be read
        # again. The mark may therefore advance only as far as the oldest row
        # in any batch that did not finish.
        barrier: datetime | None = None
        finished: list[datetime] = []
        every: list[datetime] = []
        for directory, uris in group_by_directory(changed).items():
            outcome = await self._run_batch(directory, uris, report)
            if outcome.newest is not None:
                every.append(outcome.newest)
                if outcome.complete:
                    finished.append(outcome.newest)
            if not outcome.complete and outcome.oldest is not None:
                barrier = min(barrier or outcome.oldest, outcome.oldest)

        below = [when for when in finished if barrier is None or when < barrier]
        newest = max(below) if below else watermark.last_seen

        annotate(
            {
                "ov_ext.reflect.batches": report.batches,
                "ov_ext.reflect.proposed": report.proposed,
                "ov_ext.reflect.written": report.written,
                "ov_ext.reflect.contradictions": report.contradictions,
                "ov_ext.reflect.failures": report.failures,
            }
        )

        if newest > watermark.last_seen:
            annotate({"ov_ext.reflect.outcome": "ok"})
            return report, watermark.advanced_to(newest, now=moment)

        # Read something, advanced nothing: a batch at or below the mark
        # failed. Hold, and count it -- but not forever, or one poisoned batch
        # blocks every memory behind it for good.
        if watermark.stalls + 1 > settings.max_stalls:
            forced = max(every) if every else watermark.last_seen
            report.stepped_over = True
            logger.error(
                "ov-ext reflect: no progress for %d sweeps; stepping the watermark "
                "over %s to %s. The memories in the failing batch will NOT be "
                "reflected on. Failures this sweep: %d.",
                watermark.stalls + 1,
                watermark.last_seen.isoformat(),
                forced.isoformat(),
                report.failures,
            )
            annotate({"ov_ext.reflect.outcome": "stepped_over"})
            return report, watermark.stepped_over(forced, now=moment)

        annotate({"ov_ext.reflect.outcome": "stalled"})
        return report, watermark.stalled(now=moment)

    @traced("ov_ext.reflect.batch")
    async def _run_batch(
        self, directory: str, uris: Sequence[str], report: SweepReport
    ) -> BatchOutcome:
        """Reflect on one directory's changed memories.

        Returns the newest ``updated_at`` among the changed rows actually
        read, so the caller can advance the watermark to what was seen rather
        than to the wall clock -- ``updated_at`` because that is what
        ``changed_since`` filters on, and advancing on ``created_at`` would
        leave an edited old memory permanently above the mark, re-reflected
        every sweep forever.

        ``None`` when the batch produced nothing to advance on, which includes
        a model call that failed: marking that batch done would drop those
        memories from reflection permanently over a transient outage.
        """
        report.batches += 1
        annotate(
            {"ov_ext.reflect.directory": directory, "ov_ext.reflect.changed": len(uris)}
        )

        changed_rows = await self._store.rows(uris)
        if not changed_rows:
            annotate({"ov_ext.reflect.outcome": "no_rows"})
            return BatchOutcome(complete=True, newest=None, oldest=None)

        gathered = await self._gather(changed_rows)
        rows_by_uri = {row.uri: row for row in gathered}
        uri_to_index, index_to_uri = citation_map([row.uri for row in gathered])
        contexts = build_memory_context(gathered, uri_to_index)
        annotate({"ov_ext.reflect.gathered": len(gathered)})

        scope = await self._store.read_overview(directory)
        proposed_ok = await self._propose(
            contexts, scope, index_to_uri, rows_by_uri, report
        )

        contradicted_ok = True
        if self._settings.contradictions:
            contradicted_ok = await self._contradict(contexts, index_to_uri, report)

        oldest = min(row.updated_at for row in changed_rows)
        newest = max(row.updated_at for row in changed_rows)
        complete = proposed_ok and contradicted_ok
        if not complete:
            annotate({"ov_ext.reflect.outcome": "batch_incomplete"})
        return BatchOutcome(complete=complete, newest=newest, oldest=oldest)

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
    ) -> bool:
        """Ask for observations, verify them, write survivors; report success.

        Returns
        -------
        bool
            False when the model call failed or returned nothing parseable, so
            the caller leaves the watermark where it was and the batch is tried
            again.
        """
        settings = self._settings
        prompt = propose_prompt(contexts, scope=scope)

        try:
            proposed = await self._llm.complete(prompt, ProposedObservations)
        except Exception as exc:  # the model is a network call; a batch may fail
            record_error(exc, "propose_failed", NAMESPACE)
            report.failures += 1
            return False

        if proposed is None:
            annotate({"ov_ext.reflect.outcome": "propose_unparsed"})
            report.failures += 1
            return False

        report.proposed += len(proposed.observations)
        kept, dropped = verify_observations(
            proposed.observations,
            index_to_uri,
            rows_by_uri,
            min_evidence=settings.min_evidence,
            require_cross_area=settings.require_cross_area,
        )
        report.record_drops(dropped)

        for observation in kept:
            await self._write(observation, report)
        return True

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
    ) -> bool:
        """Ask which memories are in tension; record an edge per pair.

        Returns
        -------
        bool
            False when the model call failed or returned nothing parseable.
        """
        try:
            found = await self._llm.complete(
                contradiction_prompt(contexts),
                Contradictions,
            )
        except Exception as exc:
            record_error(exc, "contradict_failed", NAMESPACE)
            report.failures += 1
            return False

        if found is None:
            annotate({"ov_ext.reflect.outcome": "contradict_unparsed"})
            report.failures += 1
            return False

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
        return True
