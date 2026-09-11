"""The sweep, end to end, against fakes.

Covers what a sweep does with what the model gives it -- including the cases
where the model gives it nothing useful, which is the common one.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any
from datetime import timedelta

from ov_ext.reflect.config import ReflectSettings
from ov_ext.reflect.engine import ReflectionEngine, SweepReport, group_by_directory
from ov_ext.reflect.models import (
    CandidateObservation,
    Consolidation,
    EvidenceItem,
    Observation,
    ObservationGroup,
    MemoryRow,
    ProposedObservations,
)
from ov_ext.reflect.watermark import Watermark

from .helpers import EPOCH, FakeLLM, FakeStore, row

A = "viking://user/j/memories/entities/a.md"
B = "viking://user/j/memories/entities/b.md"


def settings(**overrides: object) -> ReflectSettings:
    """Build settings with reflection on and the environment ignored."""
    base: dict[str, object] = {
        "enabled": True,
        "neighbour_limit": 0,
        "tail_sample": 0,
    }
    base.update(overrides)
    return ReflectSettings.model_construct(**{**ReflectSettings().model_dump(), **base})


def proposed(*quotes: tuple[int, str]) -> ProposedObservations:
    """Build a model reply citing the given (index, quote) pairs."""
    return ProposedObservations(
        observations=[
            CandidateObservation(
                title="both retry",
                content="Both components retry.",
                evidence=[
                    EvidenceItem(memory_index=i, quote=q, relevance_explanation="r")
                    for i, q in quotes
                ],
            )
        ]
    )


def store() -> FakeStore:
    """Two memories, one day apart, in the same directory."""
    return FakeStore(
        [
            row(A, "The scheduler retries failed jobs.", day=1),
            row(B, "The worker retries failed jobs too.", day=2),
        ]
    )


async def test_a_verified_observation_is_written_and_linked_to_its_sources() -> None:
    fake = store()
    engine = ReflectionEngine(
        fake,
        FakeLLM([proposed((0, "retries failed jobs"), (1, "retries failed jobs"))]),
        settings(),
    )

    report, mark = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.written == 1
    assert [link[2] for link in fake.links] == ["derived_from", "derived_from"]
    assert {link[3] for link in fake.links} == {"retries failed jobs"}
    # The watermark follows what was read, not the clock, so a memory written
    # mid-sweep is picked up next time rather than skipped.
    assert mark.last_seen == EPOCH + timedelta(days=2)


async def test_an_unsupported_observation_never_reaches_the_store() -> None:
    fake = store()
    engine = ReflectionEngine(
        fake,
        FakeLLM([proposed((0, "retries failed jobs"), (1, "invented span"))]),
        settings(),
    )

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.proposed == 1
    assert report.written == 0
    assert fake.written == []
    assert fake.links == []
    assert report.dropped["quote_not_found"] == 1


async def test_nothing_changed_leaves_the_watermark_where_it_was() -> None:
    fake = store()
    engine = ReflectionEngine(fake, FakeLLM([]), settings())
    start = Watermark.beginning().advanced_to(EPOCH + timedelta(days=9), now=EPOCH)

    report, mark = await engine.sweep(start, now=EPOCH)

    assert report.batches == 0
    assert mark.last_seen == start.last_seen


async def test_a_failed_batch_leaves_its_memories_for_the_next_sweep() -> None:
    """A transient outage must not drop memories from reflection permanently.

    The watermark is how a memory is remembered as done. Advancing it past a
    batch whose model call failed means those memories are never reflected on
    again -- a 500 from the provider costing them forever.
    """
    fake = store()
    engine = ReflectionEngine(fake, FakeLLM([RuntimeError("upstream down")]), settings())

    report, mark = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.failures == 1
    assert report.written == 0
    assert mark.last_seen == Watermark.beginning().last_seen

    # And a later sweep really does see them again.
    retry = ReflectionEngine(
        fake,
        FakeLLM([proposed((0, "retries failed jobs"), (1, "retries failed jobs"))]),
        settings(),
    )
    retry_report, _ = await retry.sweep(mark, now=EPOCH)
    assert retry_report.written == 1


async def test_an_unparseable_reply_is_a_failure_not_a_crash() -> None:
    engine = ReflectionEngine(store(), FakeLLM([None]), settings())
    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)
    assert report.failures == 1


async def test_dry_run_reports_what_it_would_write_and_writes_nothing() -> None:
    fake = store()
    engine = ReflectionEngine(
        fake,
        FakeLLM([proposed((0, "retries failed jobs"), (1, "retries failed jobs"))]),
        settings(dry_run=True),
    )

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.written == 1
    assert fake.written == []
    assert fake.links == []


async def test_neighbours_and_tail_widen_what_the_model_sees() -> None:
    """Evidence can be cited from memories the change set never mentioned.

    This is the difference between reflection and extraction: without the
    neighbour and tail passes the model only ever sees the window that changed.
    """
    old = row(
        "viking://user/j/memories/entities/old.md", "An older note on retries.", day=-30
    )
    tail = row(
        "viking://user/j/memories/entities/tail.md", "A stray note on retries.", day=-90
    )
    fake = FakeStore(
        [row(A, "The scheduler retries failed jobs.", day=1)],
        neighbours={A: [old]},
        tail=[tail],
    )
    llm = FakeLLM(
        [proposed((0, "retries failed jobs"), (1, "older note"), (2, "stray note"))]
    )
    engine = ReflectionEngine(
        fake, llm, settings(neighbour_limit=4, tail_sample=2, min_evidence=3)
    )

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.written == 1
    cited = {link[1] for link in fake.links}
    assert old.uri in cited and tail.uri in cited


async def test_the_overview_is_offered_as_background_and_never_cited() -> None:
    """L1 is model-written, so a quote verified against it proves nothing."""
    fake = FakeStore(
        [row(A, "The scheduler retries failed jobs.", day=1)],
        overview="This area is about retry policy.",
    )
    llm = FakeLLM([proposed((0, "retries failed jobs"))])
    engine = ReflectionEngine(fake, llm, settings(min_evidence=1))

    await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert "This area is about retry policy." in llm.prompts[0]
    assert all(link[1] != "overview" for link in fake.links)
    assert {link[1] for link in fake.links} == {A}


async def test_the_batch_limit_truncates_the_newest_not_the_oldest() -> None:
    """Truncation must be resumable, or the remainder is lost forever.

    Rows arrive oldest first, so the limit cuts the newest and the watermark
    advances only as far as this batch reached. Cutting the oldest instead
    would move the mark past them and they would never be read again.
    """
    rows = [
        row(f"viking://user/j/memories/entities/{i}.md", f"note {i}", day=i)
        for i in range(1, 11)
    ]
    fake = FakeStore(rows)
    llm = FakeLLM([ProposedObservations(observations=[])])
    engine = ReflectionEngine(fake, llm, settings(batch_limit=3))

    report, mark = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.batches == 1
    assert mark.last_seen == EPOCH + timedelta(days=3)

    # The seven the limit cut are still ahead of the mark.
    remaining = await fake.changed_since(mark.last_seen, limit=100)
    assert len(remaining) == 7


async def test_a_disabled_sweep_writes_nothing() -> None:
    """The last guard between "off by default" and unattended writes.

    Registration checks the flag, but a cron or CLI can reach the engine
    without ever calling install().
    """
    fake = store()
    llm = FakeLLM([proposed((0, "retries failed jobs"), (1, "retries failed jobs"))])
    engine = ReflectionEngine(fake, llm, settings(enabled=False))

    report, mark = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.written == 0
    assert fake.written == []
    assert llm.prompts == []
    assert mark.last_seen == Watermark.beginning().last_seen


async def test_an_edited_old_memory_stops_being_re_reflected() -> None:
    """The watermark must advance on the column it filters on.

    A memory created long ago but edited today is above the mark by
    `updated_at`. Advancing on `created_at` would leave the mark in the past,
    so the same batch, the same model calls and the same write would repeat
    every sweep forever.
    """
    # Both rows are old by creation and recent by edit, and their creation
    # dates differ from their edit dates. Advancing on `created_at` would put
    # the mark at day -400, leaving both above it forever.
    fake = FakeStore(
        [
            row(A, "The scheduler retries failed jobs.", day=-500, updated=2),
            row(B, "The worker retries failed jobs too.", day=-400, updated=3),
        ]
    )
    llm = FakeLLM([proposed((0, "retries failed jobs"), (1, "retries failed jobs"))])
    engine = ReflectionEngine(fake, llm, settings())

    _, mark = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert mark.last_seen == EPOCH + timedelta(days=3)
    assert await fake.changed_since(mark.last_seen, limit=100) == []


def test_memories_are_grouped_by_their_directory() -> None:
    grouped = group_by_directory(
        [
            "viking://user/j/memories/entities/a.md",
            "viking://user/j/memories/entities/b.md",
            "viking://user/j/memories/preferences/c.md",
        ]
    )
    assert set(grouped) == {
        "viking://user/j/memories/entities",
        "viking://user/j/memories/preferences",
    }
    assert len(grouped["viking://user/j/memories/entities"]) == 2


async def test_a_failed_batch_holds_back_a_later_one_that_succeeded() -> None:
    """Batches are grouped by directory, so they are not in time order.

    Without a barrier, a newer directory that succeeded drags the mark past an
    older one that failed, and those memories are never read again.
    """
    alpha = "viking://user/j/memories/alpha/a.md"
    beta = "viking://user/j/memories/beta/b.md"
    fake = FakeStore([row(alpha, "alpha note", day=1), row(beta, "beta note", day=2)])
    # Alpha is proposed first (oldest first) and fails; beta then succeeds.
    llm = FakeLLM([RuntimeError("upstream down"), ProposedObservations(observations=[])])
    engine = ReflectionEngine(fake, llm, settings())

    report, mark = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.failures == 1
    assert mark.last_seen == Watermark.beginning().last_seen
    assert set(await fake.changed_since(mark.last_seen, limit=100)) == {alpha, beta}


async def test_a_batch_that_always_fails_does_not_block_forever() -> None:
    """Holding the mark back is right; holding it back forever is its own bug.

    memex bounds this with a queue and a dead-letter table. Here it is a stall
    counter: past `max_stalls` the sweep steps over the blockage and says so.
    """
    fake = FakeStore([row(A, "poisoned", day=1)])
    mark = Watermark.beginning()
    engine = ReflectionEngine(
        fake, FakeLLM([RuntimeError("always")] * 10), settings(max_stalls=2)
    )

    for expected_stalls in (1, 2):
        report, mark = await engine.sweep(mark, now=EPOCH)
        assert mark.stalls == expected_stalls
        assert report.stepped_over is False
        assert mark.last_seen == Watermark.beginning().last_seen

    report, mark = await engine.sweep(mark, now=EPOCH)

    assert report.stepped_over is True
    assert mark.last_seen == EPOCH + timedelta(days=1)
    assert mark.stalls == 0
    assert await fake.changed_since(mark.last_seen, limit=100) == []


async def test_progress_clears_a_stall() -> None:
    """So an intermittent failure never accumulates toward stepping over."""
    fake = FakeStore([row(A, "note", day=1)])
    stalled = Watermark.beginning().stalled(now=EPOCH).stalled(now=EPOCH)
    assert stalled.stalls == 2

    engine = ReflectionEngine(
        fake, FakeLLM([ProposedObservations(observations=[])]), settings()
    )
    _, mark = await engine.sweep(stalled, now=EPOCH)

    assert mark.last_seen == EPOCH + timedelta(days=1)
    assert mark.stalls == 0


class StoreWhoseRowsVanished(FakeStore):
    """A store whose change query names URIs its row fetch cannot return.

    Not a contrivance: the two run different queries against the index, so a
    row dropped between them -- or a backend not storing content -- lands here.
    """

    async def rows(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Return nothing, whatever was asked for."""
        return []


async def test_a_sweep_that_reads_only_empty_batches_holds_rather_than_churning() -> None:
    """`changed_since` naming URIs that `rows` cannot fetch is a stuck store.

    Stepping the mark over would clear the stall and rewrite the watermark file
    on every sweep forever, while fixing nothing -- there is no timestamp to
    step to, because nothing was read.
    """
    fake = StoreWhoseRowsVanished(
        [
            row(A, "The scheduler retries failed jobs.", day=1),
            row(B, "The worker retries failed jobs too.", day=2),
        ]
    )
    engine = ReflectionEngine(fake, FakeLLM([]), settings(max_stalls=1))
    mark = Watermark(last_seen=EPOCH, swept_at=EPOCH, stalls=5)

    report, after = await engine.sweep(mark, now=EPOCH)

    assert report.stepped_over is False
    assert after == mark, "an unadvanceable sweep must not rewrite the mark"


async def test_a_failed_pass_costs_only_the_memories_it_was_shown() -> None:
    """Requiring every pass to succeed makes recovery cube with the failure rate.

    At a 70% call failure rate that is a median of 26 sweeps rather than 2, and
    on the delta path the batch is the whole sweep -- so one bad call would cost
    everything. A failed pass means one sample went unlooked-at, not that a
    memory was missed.
    """
    rows = [
        row(f"viking://user/j/memories/entities/x/{i}.md", f"note {i}", day=i + 1)
        for i in range(4)
    ]
    # The delta store, because that is where retirement means anything:
    # `VikingStore.mark_reflected` returns early without one, so a whole-memory
    # fake would record a retirement production never makes.
    fake = DeltaReadingStore(rows)
    engine = ReflectionEngine(
        fake,
        FakeLLM([ProposedObservations(observations=[]), RuntimeError("down")]),
        settings(pass_size=2, passes=2),
        rng=Samples((0, 1), (2, 3)),
    )

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.failures == 1
    assert 0 < len(fake.reflected) < 4, (
        "the successful pass retires its own, and only its own"
    )


async def test_a_group_naming_one_observation_twice_writes_it_once() -> None:
    """`[1, 2, 2]` where 1 is already claimed left two copies of observation 2.

    Two writes and two link calls for one claim, and a `consolidated` count
    that goes negative.
    """
    made = [
        Observation(
            title=f"o{i}",
            content=f"content {i}",
            evidence=((f"viking://user/j/memories/entities/x/{i}.md", f"q{i}"),),
            areas=frozenset({"viking://user/j/memories/entities/x"}),
        )
        for i in range(3)
    ]
    engine = ReflectionEngine(
        store(),
        FakeLLM(
            [
                Consolidation(
                    groups=[
                        ObservationGroup(indices=[0, 1], title="first", content="c"),
                        ObservationGroup(indices=[1, 2, 2], title="second", content="c"),
                    ]
                )
            ]
        ),
        settings(),
    )
    report = SweepReport()

    merged = await engine._consolidate(made, report)

    titles = [o.title for o in merged]
    assert len(titles) == len(set(titles)), f"an observation was kept twice: {titles}"
    assert report.consolidated >= 0, "the metric must not go negative"


async def test_a_healthy_whole_memory_sweep_advances_the_watermark() -> None:
    """Without a delta store the watermark is the only record of progress.

    `mark_reflected` is a no-op there, so if a sweep where every call succeeded
    reports no progress, the stall counter climbs and the step-over eventually
    forces the mark past memories no pass ever read. Those are gone for good.

    Sampling draws from the changed memories *plus* their neighbours, so most
    changed URIs are in no sample even when nothing fails -- which is why
    coverage cannot be what "complete" means on this path.
    """
    changed = [
        row(f"viking://user/j/memories/entities/x/{i}.md", f"note {i}", day=i + 1)
        for i in range(10)
    ]
    neighbours = {
        r.uri: [
            row(f"viking://user/j/memories/entities/y/{i}.md", f"near {i}", day=1)
            for i in range(8)
        ]
        for r in changed
    }
    fake = FakeStore(changed, neighbours=neighbours)
    engine = ReflectionEngine(
        fake,
        FakeLLM([ProposedObservations(observations=[])] * 8),
        settings(pass_size=12, passes=3, neighbour_limit=8),
        rng=random.Random(7),
    )

    report, after = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.failures == 0
    assert after.stalls == 0, "a sweep where nothing failed has made progress"
    assert after.last_seen > Watermark.beginning().last_seen


class Samples:
    """Hands out the exact samples a case needs, in order.

    `self._rng` is used for one call in the whole engine, so replacing it says
    what a test means instead of leaning on CPython's Mersenne Twister to
    supply it -- and a reader can see *why* these particular samples matter.
    """

    def __init__(self, *picks: tuple[int, ...]) -> None:
        self._picks = list(picks)

    def sample(self, population: list[Any], k: int) -> list[Any]:
        """Return the next configured pick, by position in ``population``."""
        return [population[i] for i in self._picks.pop(0)]


class DeltaReadingStore(FakeStore):
    """A store that reads captured changes, where pendingness records progress."""

    @property
    def reads_deltas(self) -> bool:
        return True


async def test_a_delta_sweep_that_covered_everything_advances() -> None:
    """On this path coverage IS completion: what was read has just been retired.

    Holding the mark because a pass failed would be the cubic recovery again --
    and here there is nothing to recover, because every memory was read.
    """
    changed = [
        row(f"viking://user/j/memories/entities/x/{i}.md", f"note {i}", day=i + 1)
        for i in range(3)
    ]
    fake = DeltaReadingStore(changed)
    engine = ReflectionEngine(
        fake,
        FakeLLM([ProposedObservations(observations=[])] * 4),
        # Everything fits one call, so every URI is covered.
        settings(pass_size=12, passes=3),
        rng=random.Random(3),
    )

    report, after = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.failures == 0
    assert sorted(fake.reflected) == sorted(r.uri for r in changed)
    assert after.stalls == 0


async def test_a_delta_sweep_covered_by_the_passes_that_worked_still_advances() -> None:
    """The case where the two rules disagree.

    Two passes between them saw every memory; a third failed. Nothing is
    outstanding -- everything was read and retired -- so treating the failed
    pass as reason to hold would be the cubic recovery again, for a batch with
    nothing left to recover.
    """
    changed = [
        row(f"viking://user/j/memories/entities/x/{i}.md", f"note {i}", day=i + 1)
        for i in range(4)
    ]
    fake = DeltaReadingStore(changed)
    engine = ReflectionEngine(
        fake,
        FakeLLM(
            [
                ProposedObservations(observations=[]),
                ProposedObservations(observations=[]),
                RuntimeError("the third pass fell over"),
            ]
        ),
        settings(pass_size=3, passes=3),
        # Between them the first two passes see every memory; the third falls
        # over having seen nothing new.
        rng=Samples((0, 1, 3), (1, 2, 3), (0, 1, 2)),
    )

    report, after = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.failures == 1
    assert sorted(fake.reflected) == sorted(r.uri for r in changed)
    assert after.stalls == 0, "nothing is outstanding, so this is not a stall"
