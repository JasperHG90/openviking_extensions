"""The sweep, end to end, against fakes.

Covers what a sweep does with what the model gives it -- including the cases
where the model gives it nothing useful, which is the common one.
"""

from __future__ import annotations

from datetime import timedelta

from ov_ext.reflect.config import ReflectSettings
from ov_ext.reflect.engine import ReflectionEngine, group_by_directory
from ov_ext.reflect.models import (
    CandidateObservation,
    ContradictionRelationship,
    Contradictions,
    EvidenceItem,
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
        "contradictions": False,
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


async def test_contradictions_are_recorded_as_links_between_the_pair() -> None:
    fake = store()
    llm = FakeLLM(
        [
            ProposedObservations(observations=[]),
            Contradictions(
                relationships=[
                    ContradictionRelationship(
                        left_index=0, right_index=1, relation="contradict", reasoning="r"
                    )
                ]
            ),
        ]
    )
    engine = ReflectionEngine(fake, llm, settings(contradictions=True))

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.contradictions == 1
    # Changed memories arrive oldest first, so A is index 0 and B is index 1 --
    # the edge runs in the direction the model named.
    assert fake.links == [(A, B, "contradicts", None, 0.9)]
    # No match_text: the claim is about two memories as wholes, and OpenViking
    # checks match_text verbatim, so inventing a span would fail that check.
    assert fake.links[0][3] is None


async def test_agreement_is_not_worth_an_edge() -> None:
    """`reinforce` is the normal state of a store; linking it would drown the rest."""
    fake = store()
    llm = FakeLLM(
        [
            ProposedObservations(observations=[]),
            Contradictions(
                relationships=[
                    ContradictionRelationship(
                        left_index=0, right_index=1, relation="reinforce", reasoning="r"
                    )
                ]
            ),
        ]
    )
    engine = ReflectionEngine(fake, llm, settings(contradictions=True))

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.contradictions == 0
    assert fake.links == []


async def test_a_contradiction_citing_itself_or_nothing_is_dropped() -> None:
    fake = store()
    llm = FakeLLM(
        [
            ProposedObservations(observations=[]),
            Contradictions(
                relationships=[
                    ContradictionRelationship(
                        left_index=0,
                        right_index=0,
                        relation="contradict",
                        reasoning="self",
                    ),
                    ContradictionRelationship(
                        left_index=0,
                        right_index=42,
                        relation="contradict",
                        reasoning="ghost",
                    ),
                ]
            ),
        ]
    )
    engine = ReflectionEngine(fake, llm, settings(contradictions=True))

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.contradictions == 0
    assert fake.links == []


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
