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
6. write what survived -- revising the observation already filed under that
   memory, when there is one -- and move the watermark.

Step 6 is where an observation becomes a thing that evolves rather than a
snapshot. A file is named after the memory it is about, so the next sweep with
something to say about that memory opens the file that is already there, hands
it to the model beside the new claim, and writes back what holds now. Naming a
file after the evidence instead is how one running observation about a scraper
became thirteen files, each true, none of them the current answer.

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
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..observability import annotate, record_error, traced
from .citations import build_memory_context, citation_map
from .config import ReflectSettings
from .exceptions import ObservationUnreadableError
from .models import (
    Consolidation,
    MemoryRow,
    Observation,
    ProposedObservations,
    ReflectMemoryContext,
    Revision,
)
from .ports import MemoryStore, Sampler, StructuredLLM
from .prompts import consolidate_prompt, propose_prompt, revise_prompt
from .verify import areas_of, normalise, quote_is_present, verify_observations
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
    revised : int
        Observations folded into a file that already stood, rather than
        starting one. Rises as a store matures: the more a subject has been
        reflected on, the likelier the next sweep revises instead of creating.
    unwritten : int
        Verified observations that survived every gate and still did not reach
        the store, because the file they belong in could not be read or could
        not be revised. Counted apart from ``failures`` because nothing was
        abandoned and nothing was lost: the changes behind them stay pending
        and are read again. A number that stays high means one subject's file
        is stuck.
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
    revised: int = 0
    unwritten: int = 0
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
        rng: Sampler | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._settings = settings or ReflectSettings()
        # Unseeded on purpose: two sweeps sampling identically would ask the
        # same questions twice. Tests inject their own.
        self._rng: Sampler = rng or random.Random()

    @property
    def _reads_deltas(self) -> bool:
        """Whether the store reads captured changes rather than whole memories.

        Changes what "no progress" means: with deltas, pendingness tracks the
        work and the watermark tracks nothing, so a stalled sweep has skipped
        no memory however long it stalls.
        """
        return getattr(self._store, "reads_deltas", False)

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
                "ov_ext.reflect.revised": report.revised,
                "ov_ext.reflect.unwritten": report.unwritten,
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
            if self._reads_deltas:
                # The watermark is not what tracks progress here -- pendingness
                # is, and a failed batch retires nothing -- so nothing is being
                # stepped over and no memory is dropped. Said plainly, because
                # the operator most likely to read this is one debugging the
                # outage that caused it.
                logger.error(
                    "ov-ext reflect: no progress for %d sweeps. Reading captured "
                    "changes, so the watermark is unused and NOTHING has been "
                    "skipped -- every pending change is still pending and will be "
                    "read again. Failures this sweep: %d, observations that could "
                    "not be written: %d.",
                    watermark.stalls + 1,
                    report.failures,
                    report.unwritten,
                )
                annotate({"ov_ext.reflect.outcome": "stepped_over"})
                return report, watermark.stepped_over(forced, now=moment)
            logger.error(
                "ov-ext reflect: no progress for %d sweeps; stepping the watermark "
                "over %s to %s. The memories in the failing batch will NOT be "
                "reflected on. Failures this sweep: %d, observations that could "
                "not be written: %d.",
                watermark.stalls + 1,
                watermark.last_seen.isoformat(),
                forced.isoformat(),
                report.failures,
                report.unwritten,
            )
            annotate({"ov_ext.reflect.outcome": "stepped_over"})
            return report, watermark.stepped_over(forced, now=moment)

        annotate({"ov_ext.reflect.outcome": "stalled"})
        logger.warning(
            "ov-ext reflect: read %d changed memories across %d batches but "
            "advanced nothing; watermark held at %s, stall %d of %d. Failures "
            "this sweep: %d, observations that could not be written: %d. A "
            "sweep that keeps reporting unwritten observations has a subject "
            "whose file cannot be read or revised -- the log above names it.",
            len(changed),
            report.batches,
            watermark.last_seen.isoformat(),
            watermark.stalls + 1,
            settings.max_stalls,
            report.failures,
            report.unwritten,
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
        settings = self._settings
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
        kept, covered, every_pass_ran = await self._passes(
            contexts, scope, index_to_uri, rows_by_uri, report
        )
        # Written before anything is retired, so a sweep that dies here reads
        # the same changes again rather than dropping them. A re-read is cheap
        # now that a subject has one file: the second pass revises what the
        # first wrote instead of landing beside it.
        #
        # `uris` -- the memories this batch is reflecting *on* -- decides what
        # each observation is filed under. Without it the file is chosen from
        # whichever neighbours the sample drew, which is not stable between
        # sweeps; see `VikingStore._primary`.
        subjects = set(uris)
        for observation in await self._consolidate(kept, report):
            if not await self._write(observation, report, subjects):
                # Only the changes THIS observation was drawn from stay pending.
                # Clearing the whole batch instead livelocks the delta path,
                # where `group` pools every change into one batch: one stuck
                # subject would retire nothing for the entire sweep, forever,
                # and drag every healthy subject back through a revise call
                # every tick. The failure is per-observation, so the
                # bookkeeping has to be too.
                covered -= observation.sources

        # A URI nothing read stays pending. On the delta path that is the whole
        # record of outstanding work, so it is read again next sweep.
        #
        # Nothing is retired when a write did not land: the delta is the only
        # record that this change still needs reflecting on, and retiring it
        # because the batch *reached* the write would lose the change to a
        # failure that has nothing to do with it.
        retiring = [uri for uri in uris if uri in covered]
        if retiring and not settings.dry_run:
            # A dry run writes nothing, so it has reflected on nothing, so it
            # must retire nothing. The watermark is already held back for it
            # (`runner.run_sweep`); pendingness is the other record of progress
            # and was not, so previewing a prompt change against a delta store
            # silently consumed the queue it was previewing -- and switching
            # the dry run off then found every one of those changes gone.
            await self._store.mark_reflected(retiring)
        missed = len(uris) - len(retiring)
        if missed:
            logger.info(
                "ov-ext reflect: %d of %d changed memories went unread this "
                "batch; they stay pending for the next sweep",
                missed,
                len(uris),
            )

        # What "complete" means depends on what records progress.
        #
        # With deltas, pendingness does, and `mark_reflected` has just retired
        # exactly what was read -- so the batch is complete when nothing was
        # missed, and anything left over is picked up next sweep.
        #
        # Without them the watermark is the only record, and `mark_reflected` is
        # a no-op. Coverage cannot mean completion there: a sample is drawn from
        # the changed memories *plus* their neighbours, so most changed URIs are
        # in no sample even when every call succeeds. Reporting that as no
        # progress makes a healthy sweep stall, and the step-over then forces
        # the mark past memories nothing ever read -- losing them for good, on
        # the default configuration, to fix a problem that path never had.
        complete = (not missed) if self._reads_deltas else every_pass_ran

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
    ) -> tuple[list[Observation], set[str], bool]:
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
        tuple[list[Observation], set[str], bool]
            What survived verification, the URIs a successful pass was shown,
            and whether every pass ran.

            Both of the last two are reported because the two store kinds judge
            a batch differently. With deltas, coverage is what matters:
            requiring every pass to succeed makes recovery cube with the failure
            rate -- a median of 26 sweeps rather than 2 at a 70% failure rate --
            and the batch is the whole sweep, so one bad call would cost
            everything. A failed pass means one sample went unlooked-at, not
            that a memory was missed.

            Without deltas the watermark is the only record of progress, and
            coverage cannot speak to it: a sample is drawn from the changed
            memories *plus* their neighbours, so most changed URIs are in no
            sample even when nothing fails. There the question is only whether
            every pass ran.
        """
        settings = self._settings
        if len(contexts) <= settings.pass_size:
            # Everything fits in one call. Sampling would show the model the
            # same memories in a different order and charge for it.
            kept = await self._propose(contexts, scope, index_to_uri, rows_by_uri, report)
            if kept is None:
                return [], set[str](), False
            return (
                kept,
                {
                    index_to_uri[c.index_id]
                    for c in contexts
                    if c.index_id in index_to_uri
                },
                True,
            )

        pooled: list[Observation] = []
        covered: set[str] = set()
        every_pass_ran = True
        for number in range(settings.passes):
            sample = self._rng.sample(list(contexts), settings.pass_size)
            sample.sort(key=lambda context: context.index_id)
            annotate({"ov_ext.reflect.pass": number + 1})
            kept = await self._propose(sample, scope, index_to_uri, rows_by_uri, report)
            if kept is None:
                every_pass_ran = False
                continue
            pooled.extend(kept)
            covered |= {
                index_to_uri[c.index_id] for c in sample if c.index_id in index_to_uri
            }
        return pooled, covered, every_pass_ran

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
            # Deduped, order kept. A group naming an index twice would
            # otherwise carry the same observation twice into `merged` -- two
            # writes and two link calls for one claim, and a `consolidated`
            # count that goes negative.
            wanted = list(
                dict.fromkeys(
                    index for index in group.indices if 0 <= index < len(observations)
                )
            )
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

    async def _write(
        self,
        observation: Observation,
        report: SweepReport,
        subjects: Collection[str] = (),
    ) -> bool:
        """Persist one observation and link it to every memory it cites.

        An observation is filed under the memory it is about, so a subject
        reflected on twice resolves to the file it already has. When one is
        there, this revises it rather than writing beside it -- which is the
        whole difference between an observation that evolves and a directory
        full of snapshots nobody will ever reconcile.

        Parameters
        ----------
        observation :
            The verified observation to file.
        report :
            Counters for what was written, revised, and lost.
        subjects :
            The memories this batch reflected on, which is what the observation
            is filed under.

        Returns
        -------
        bool
            Whether the observation reached the store. ``False`` means the
            change it came from has not been reflected on and must stay pending.
        """
        try:
            uri, standing = await self._store.resolve(observation, subjects)
        except ObservationUnreadableError as exc:
            # What is at `uri` is unknown, so writing would risk destroying a
            # file that is still there. Skipped, and reported as unwritten so
            # the change stays pending.
            record_error(exc, "standing_unreadable", NAMESPACE)
            logger.warning(
                "ov-ext reflect: %s, so it is left untouched and the change "
                "from %r stays pending",
                exc,
                observation.title,
                exc_info=exc,
            )
            report.unwritten += 1
            return False

        if self._settings.dry_run:
            # Resolved first, and reported as the file it would really land in.
            # Naming the best-ranked file instead hid the one thing a dry run is
            # for: a claim that would be folded into an observation already
            # standing somewhere else.
            logger.info(
                "ov-ext reflect (dry run): would %s %s with %r, citing %d memories",
                "revise" if standing is not None else "write",
                uri,
                observation.title,
                len(observation.evidence),
            )
            report.written += 1
            if standing is not None:
                report.revised += 1
            return True

        if standing is not None:
            revised = await self._revise(standing, observation, uri, report)
            if revised is None:
                # The revise call failed. The standing file already covers this
                # subject, so it is left exactly as it is: writing the new claim
                # to a file of its own is the sprawl this exists to end, and
                # overwriting a standing observation with one the model never
                # got to reconcile would lose what it says. Reported as
                # unwritten, so the change is read again rather than retired on
                # the strength of a write that did not happen.
                return False
            observation = revised

        uri = await self._store.write_observation(observation, uri=uri)
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
        return True

    @traced("ov_ext.reflect.revise")
    async def _revise(
        self,
        standing: Observation,
        incoming: Observation,
        uri: str,
        report: SweepReport,
    ) -> Observation | None:
        """Fold a new observation into the one already written about a subject.

        One model call, made only when a file is already there. It is given the
        two claims and no quotes: the evidence on both sides has been verified
        against the memories it cites, and asking the model to restate it would
        put text nobody checked into a file whose every line is supposed to be
        traceable. The merged evidence is attached here instead.

        Parameters
        ----------
        standing :
            The observation read back from ``uri``.
        incoming :
            What this sweep drew about the same subject.
        uri :
            The file being revised, whose name is shown to the model as the
            subject so a revision does not drift onto whichever claim it read
            last.
        report :
            Counters, for the revision and for a call that failed.

        Returns
        -------
        Observation | None
            The observation that now holds, or ``None`` when the model call
            failed -- the caller then leaves the standing file alone.
        """
        try:
            revision = await self._llm.complete(
                revise_prompt(
                    (standing.title, standing.content),
                    (incoming.title, incoming.content),
                    subject=uri.rsplit("/", 1)[-1].removesuffix(".md"),
                ),
                Revision,
            )
        except Exception as exc:  # the model is a network call; a revision may fail
            record_error(exc, "revise_failed", NAMESPACE)
            logger.warning(
                "ov-ext reflect: revise call failed; %s left as it stands",
                uri,
                exc_info=exc,
            )
            report.unwritten += 1
            return None

        if revision is None:
            annotate({"ov_ext.reflect.outcome": "revise_unparsed"})
            logger.warning(
                "ov-ext reflect: revise call returned nothing parseable; %s left "
                "as it stands",
                uri,
            )
            report.unwritten += 1
            return None

        evidence = await self._merged_evidence(incoming, standing)
        if not evidence:
            # Everything on both sides failed the freshness re-check, which the
            # delta path can reach on its own: the incoming quote was verified
            # against the captured change while the re-check reads the index,
            # so index lag alone can empty the merge. An observation with no
            # evidence is not one this store will write -- `write_observation`
            # weights each link by 1/len -- and writing it would in any case
            # replace a traceable file with an untraceable one.
            logger.warning(
                "ov-ext reflect: every quote for %s failed the freshness "
                "re-check, so there is nothing left to write; left as it stands",
                uri,
            )
            report.unwritten += 1
            return None
        report.revised += 1
        return Observation(
            # Falling back rather than accepting an empty string: a revision
            # that came back blank in one field still carries a merge worth
            # keeping in the other, and an untitled observation would be
            # renamed from scratch by the next sweep that touched it.
            title=revision.title.strip() or standing.title,
            content=revision.content.strip() or standing.content,
            evidence=evidence,
            areas=areas_of(uri for uri, _ in evidence),
        )

    async def _merged_evidence(
        self, incoming: Observation, standing: Observation
    ) -> tuple[tuple[str, str], ...]:
        """Evidence for a revised observation: this sweep's, then what stands.

        Newest first, because the cap cuts the tail. An observation revised for
        months would otherwise keep the quotes it was founded on forever and
        drop the change that prompted the latest revision -- the wrong way
        round, since the file is supposed to say what holds now.

        Deduped on the normalised quote, exactly as verification dedupes it, so
        the same span quoted again with a line break in a different place is one
        piece of evidence and not two.

        Carried-forward quotes are checked against the memory they cite *as it
        is now*, and dropped when it no longer contains them. They were verified
        when the standing observation was written, which may have been months
        ago; a memory edited since leaves the file asserting a citation that is
        no longer true, and every revision re-asserts it. This is the same
        substring check verification runs, against freshly read rows -- the
        `max_evidence` cap bounds how many quotes a file keeps, which is not the
        same as keeping the ones that are still there.
        """
        merged: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for uri, quote in (*incoming.evidence, *standing.evidence):
            key = (uri, normalise(quote))
            if key in seen:
                continue
            seen.add(key)
            merged.append((uri, quote))

        fresh = await self._still_supported(standing.evidence)
        kept = [
            (uri, quote)
            for uri, quote in merged
            if (uri, normalise(quote)) not in fresh or fresh[(uri, normalise(quote))]
        ]
        return tuple(self._capped(kept))

    async def _still_supported(
        self, evidence: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], bool]:
        """Re-check carried-forward quotes against the memories they cite.

        Returns
        -------
        dict[tuple[str, str], bool]
            ``(uri, normalised quote)`` to whether the quote is still in the
            memory. A memory this cannot read is absent from the result, and the
            caller keeps its quotes: an index that will not answer is not
            evidence that a citation went stale.
        """
        if not evidence:
            return {}
        try:
            rows = await self._store.whole_memories(sorted({uri for uri, _ in evidence}))
        except Exception as exc:
            # Including ContentUnavailableError. Re-verification is a
            # safeguard, not the write path, and refusing to revise because the
            # check could not run would trade a stale quote for a frozen
            # observation.
            logger.warning(
                "ov-ext reflect: could not re-check carried evidence; keeping it",
                exc_info=exc,
            )
            return {}
        texts = {row.uri: row.text for row in rows}
        return {
            (uri, normalise(quote)): quote_is_present(quote, texts[uri])
            for uri, quote in evidence
            if uri in texts
        }

    def _capped(self, evidence: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        """Trim evidence to the cap without dropping below the evidence floor.

        The cap counts quotes and the floor counts distinct memories, so taking
        the first ``max_evidence`` can leave an observation resting on one
        source -- under the floor verification had just enforced, in a file that
        still claims to be a synthesis. Quotes from memories not yet represented
        are kept past the cap until the floor is met.
        """
        cap = self._settings.max_evidence
        head = list(evidence[:cap])
        sources = {uri for uri, _ in head}
        for uri, quote in evidence[cap:]:
            if len(sources) >= self._settings.min_evidence:
                break
            if uri in sources:
                continue
            sources.add(uri)
            head.append((uri, quote))
        return head
