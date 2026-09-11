"""The sweep: gather, propose, verify, write.

memex runs seven numbered phases against a ``mental_models`` table, with
compare-and-swap writes, a Postgres work queue and dead-lettering. Almost all
of that exists to make a row update safe under concurrency. OpenViking has no
such row -- an observation is a memory file behind a URI -- so what is left is
one function:

1. ask the store what changed since the watermark;
2. group by directory, so one batch is one area of the store;
3. gather evidence: the changed memories, their semantic neighbours, and a few
   at random;
4. one model call for observations;
5. verify every quote in code;
6. write what survived, and move the watermark.

There was a second model call, asking which memories were in tension, and it is
gone. Contradiction needs two whole memories held side by side to mean anything,
and a batch does not offer that: its members are grouped by directory, so the
model was asked whether a note about a package rename contradicted a quote from
a README. It answered, because it was asked to. Every pair it returned was an
artifact of the question rather than a tension in the store, and each one cost a
model call and wrote a ``contradicts`` edge into somebody's memory.

Nothing here raises on a bad batch. A directory whose model call fails, or
whose observations all fail verification, is counted and skipped -- a sweep
covering ten areas should not lose nine of them to one bad response.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..observability import annotate, record_error, traced
from .citations import build_memory_context, citation_map
from .config import ReflectSettings
from .models import (
    Consolidation,
    MemoryRow,
    Observation,
    ProposedObservations,
    ReflectMemoryContext,
)
from .ports import MemoryStore, StructuredLLM
from .prompts import consolidate_prompt, propose_prompt
from .verify import normalise, verify_observations
from .watermark import Watermark

__all__ = ["BatchOutcome", "ReflectionEngine", "SweepReport"]

logger = logging.getLogger(__name__)

# Span and attribute prefix for this subsystem, matching `ov_ext.retrieval`.
NAMESPACE = "ov_ext.reflect"


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
    consolidated : int
        Observations merged into another by the consolidation pass. High next
        to ``written`` means the passes are covering the same ground.
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
    consolidated: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    failures: int = 0
    stepped_over: bool = False

    def record_drops(self, counts: dict[str, int]) -> None:
        """Fold one batch's drop counts into the running totals."""
        for reason, count in counts.items():
            self.dropped[reason] = self.dropped.get(reason, 0) + count


def group_by_directory(uris: Sequence[str]) -> dict[str, list[str]]:
    """Group stored URIs by their parent directory, preserving order.

    How a sweep is batched when it reads whole memories: a directory is the
    unit OpenViking already summarizes, so it has an overview to use as
    background, and a directory's worth of memories is already a full prompt.

    Not used on the delta path -- see ``VikingStore.group``, which pools
    instead. Kept here because the store that reads whole memories still calls
    it, and because it is the thing the pooled version is a departure from.
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
        rng: random.Random | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._settings = settings or ReflectSettings()
        # Unseeded on purpose: two sweeps sampling identically would ask the
        # same questions twice. Tests inject their own.
        self._rng = rng or random.Random()

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
        # The store decides, because it knows whether it is reading deltas.
        for directory, uris in self._store.group(changed).items():
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
            if not every:
                # Nothing was read to step over to -- every batch resolved to no
                # rows, so `changed_since` and `rows` disagree about the store.
                # Forcing the mark to where it already is would clear the stall
                # and rewrite the file every sweep, forever, while fixing
                # nothing. Hold instead, and say why.
                logger.error(
                    "ov-ext reflect: %d sweeps with no progress and nothing to "
                    "step over to -- %d batches all read zero rows. The change "
                    "query and the row fetch disagree; reflection is stuck until "
                    "that is fixed.",
                    watermark.stalls + 1,
                    report.batches,
                )
                annotate({"ov_ext.reflect.outcome": "stuck_no_rows"})
                return report, watermark
            forced = max(every)
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
        logger.warning(
            "ov-ext reflect: read %d changed memories across %d batches but "
            "advanced nothing; watermark held at %s, stall %d of %d. Failures "
            "this sweep: %d.",
            len(changed),
            report.batches,
            watermark.last_seen.isoformat(),
            watermark.stalls + 1,
            settings.max_stalls,
            report.failures,
        )
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
            # `changed_since` just named these URIs, so finding no rows behind
            # them means the two queries disagree about the store. Warned rather
            # than passed over: a batch that reads nothing advances nothing, so
            # a sweep where every batch lands here repeats forever, and silence
            # here is what makes that look like an idle sweep.
            annotate({"ov_ext.reflect.outcome": "no_rows"})
            logger.warning(
                "ov-ext reflect: %d changed URIs in %s fetched no rows; "
                "nothing to reflect on and the watermark cannot advance",
                len(uris),
                directory,
            )
            # Read, and nothing citable in them -- a batch of pure deletions
            # looks exactly like this. Retire them anyway, or every sweep reads
            # the same changes again and never gets past them.
            await self._store.mark_reflected(uris)
            return BatchOutcome(complete=True, newest=None, oldest=None)

        gathered = await self._gather(changed_rows)
        rows_by_uri = {row.uri: row for row in gathered}
        uri_to_index, index_to_uri = citation_map([row.uri for row in gathered])
        contexts = build_memory_context(gathered, uri_to_index)
        annotate({"ov_ext.reflect.gathered": len(gathered)})

        scope = await self._store.read_overview(directory)
        kept, complete = await self._passes(
            contexts, scope, index_to_uri, rows_by_uri, report
        )
        if complete:
            # Writing an incomplete batch would write AND retry it: the deltas
            # stay pending, the next sweep reads the same changes, and
            # `write_observation` names a file after the title, which the model
            # does not repeat word for word -- so each retry lands somewhere new
            # instead of merging. Those duplicates then become evidence for
            # further observations, which is the thing consolidation exists to
            # stop. Nothing is lost by holding: the deltas are still pending, so
            # the batch runs again whole.
            for observation in await self._consolidate(kept, report):
                await self._write(observation, report)
        elif kept:
            logger.info(
                "ov-ext reflect: %d observations held back; a pass in this batch "
                "failed and the batch is retried whole next sweep",
                len(kept),
            )

        if complete:
            # Only now: a delta retired by a batch that then failed is a change
            # nothing will ever reflect on.
            # Every URI the batch was given, not only those that yielded a row:
            # one with nothing citable was still read, and leaving it pending
            # would stall the sweep on it forever.
            await self._store.mark_reflected(uris)

        oldest = min(row.updated_at for row in changed_rows)
        newest = max(row.updated_at for row in changed_rows)
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
    ) -> list[Observation] | None:
        """Ask for observations and verify them; return the survivors.

        Returns
        -------
        list[Observation] | None
            What survived verification, or ``None`` when the model call failed
            or returned nothing parseable -- so the caller leaves the watermark
            where it was and the batch is tried again.
        """
        settings = self._settings
        prompt = propose_prompt(contexts, scope=scope)

        try:
            proposed = await self._llm.complete(prompt, ProposedObservations)
        except Exception as exc:  # the model is a network call; a batch may fail
            record_error(exc, "propose_failed", NAMESPACE)
            # Logged as well as recorded on the span: `record_error` writes only
            # to the tracer, so a model that fails on every batch left nothing in
            # the logs but a `failures=` count, and an operator reading stderr saw
            # a sweep that looked idle rather than broken.
            logger.warning(
                "ov-ext reflect: propose call failed; pass skipped", exc_info=exc
            )
            report.failures += 1
            return None

        if proposed is None:
            annotate({"ov_ext.reflect.outcome": "propose_unparsed"})
            logger.warning(
                "ov-ext reflect: propose call returned nothing parseable; pass skipped"
            )
            report.failures += 1
            return None

        report.proposed += len(proposed.observations)
        kept, dropped = verify_observations(
            proposed.observations,
            index_to_uri,
            rows_by_uri,
            min_evidence=settings.min_evidence,
            require_cross_area=settings.require_cross_area,
        )
        report.record_drops(dropped)
        return kept

    async def _passes(
        self,
        contexts: Sequence[ReflectMemoryContext],
        scope: str | None,
        index_to_uri: dict[int, str],
        rows_by_uri: dict[str, MemoryRow],
        report: SweepReport,
    ) -> tuple[list[Observation], bool]:
        """Look at the batch several times, each through a different sample.

        One call sees one arrangement of the memories and finds the patterns
        that arrangement suggests. A connection two entities only make when read
        together comes up only if they land in the same call, and with a dozen
        shown out of a larger batch that is a coin toss -- so the sweep tosses
        it more than once. Overlapping samples rather than a partition, for the
        same reason: a memory is worth seeing beside more than one set of
        neighbours.

        Returns
        -------
        tuple[list[Observation], bool]
            Everything that survived verification across the passes, and whether
            every pass completed. A batch where one pass failed is incomplete,
            so its memories are tried again rather than retired half-read.
        """
        settings = self._settings
        if len(contexts) <= settings.pass_size:
            # Everything fits in one call. Sampling would show the model the
            # same memories in a different order and charge for it.
            kept = await self._propose(contexts, scope, index_to_uri, rows_by_uri, report)
            return (kept or [], kept is not None)

        pooled: list[Observation] = []
        complete = True
        for number in range(settings.passes):
            sample = self._rng.sample(list(contexts), settings.pass_size)
            sample.sort(key=lambda context: context.index_id)
            annotate({"ov_ext.reflect.pass": number + 1})
            kept = await self._propose(sample, scope, index_to_uri, rows_by_uri, report)
            if kept is None:
                complete = False
                continue
            pooled.extend(kept)
        return pooled, complete

    async def _consolidate(
        self, observations: Sequence[Observation], report: SweepReport
    ) -> list[Observation]:
        """Merge observations that several passes made in different words.

        Overlapping samples mean the same claim surfaces more than once, and
        writing each copy would fill the store with near-duplicates that then
        become evidence for further observations.

        The model returns groups of indices, titles and content -- never quotes.
        Evidence is carried over from the proposals it grouped, so every quote in
        a written observation is one that was already checked against the memory
        it cites, and consolidation cannot introduce a citation nobody verified.

        Returns
        -------
        list[Observation]
            One per group. The input unchanged when there is nothing to merge,
            or when the model call fails -- writing duplicates is a worse
            outcome than not writing at all, but only slightly, and losing
            verified work to a failed merge is worse than both.
        """
        if len(observations) < 2:
            return list(observations)

        try:
            grouped = await self._llm.complete(
                consolidate_prompt([(o.title, o.content) for o in observations]),
                Consolidation,
            )
        except Exception as exc:
            record_error(exc, "consolidate_failed", NAMESPACE)
            logger.warning(
                "ov-ext reflect: consolidation failed; writing the passes' "
                "observations unmerged",
                exc_info=exc,
            )
            return list(observations)

        if grouped is None or not grouped.groups:
            annotate({"ov_ext.reflect.outcome": "consolidate_unparsed"})
            return list(observations)

        merged: list[Observation] = []
        claimed: set[int] = set()
        for group in grouped.groups:
            wanted = [index for index in group.indices if 0 <= index < len(observations)]
            members = [observations[i] for i in wanted if i not in claimed]
            if not members:
                continue
            if len(members) < len(wanted):
                # Overlapping groups. The title and content the model wrote
                # describe every member it named, and some of those went to an
                # earlier group -- so the prose would claim more than the
                # evidence left here supports. Keep the survivors as proposed.
                merged.extend(members)
                claimed.update(wanted)
                continue
            claimed.update(wanted)
            evidence: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for member in members:
                for uri, quote in member.evidence:
                    # Compared normalised, stored raw: two passes quoting the
                    # same span with different whitespace are one piece of
                    # evidence, and counting both would dilute the link weight
                    # and write two near-identical `derived_from` edges.
                    key = (uri, normalise(quote))
                    if key in seen:
                        continue
                    seen.add(key)
                    evidence.append((uri, quote))
            merged.append(
                Observation(
                    title=group.title.strip() or members[0].title,
                    content=group.content.strip() or members[0].content,
                    evidence=tuple(evidence),
                    areas=frozenset().union(*(m.areas for m in members)),
                )
            )

        # An index the model forgot is a verified observation it would silently
        # drop. Kept as it was proposed.
        merged.extend(
            observation
            for index, observation in enumerate(observations)
            if index not in claimed
        )
        report.consolidated += len(observations) - len(merged)
        return merged

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
