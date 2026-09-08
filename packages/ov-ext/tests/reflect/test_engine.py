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
        fake, FakeLLM([proposed((0, "retries failed jobs"), (1, "retries failed jobs"))]),
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
        fake, FakeLLM([proposed((0, "retries failed jobs"), (1, "invented span"))]), settings()
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


async def test_a_model_that_fails_costs_a_batch_not_the_sweep() -> None:
    fake = store()
    engine = ReflectionEngine(fake, FakeLLM([RuntimeError("upstream down")]), settings())

    report, mark = await engine.sweep(Watermark.beginning(), now=EPOCH)

    assert report.failures == 1
    assert report.written == 0
    # The batch was read even though the model call failed, so the watermark
    # still moves: re-reading it next sweep would repeat the same failure.
    assert mark.last_seen == EPOCH + timedelta(days=2)


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
    old = row("viking://user/j/memories/entities/old.md", "An older note on retries.", day=-30)
    tail = row("viking://user/j/memories/entities/tail.md", "A stray note on retries.", day=-90)
    fake = FakeStore(
        [row(A, "The scheduler retries failed jobs.", day=1)],
        neighbours={A: [old]},
        tail=[tail],
    )
    llm = FakeLLM([proposed((0, "retries failed jobs"), (1, "older note"), (2, "stray note"))])
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
    # Changed memories arrive newest first, so B is index 0 and A is index 1 --
    # the edge runs in the direction the model named, not in URI order.
    assert fake.links == [(B, A, "contradicts", None, 0.9)]
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
                        left_index=0, right_index=0, relation="contradict", reasoning="self"
                    ),
                    ContradictionRelationship(
                        left_index=0, right_index=42, relation="contradict", reasoning="ghost"
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


async def test_the_batch_limit_bounds_one_sweep() -> None:
    rows = [row(f"viking://user/j/memories/entities/{i}.md", f"note {i}", day=i) for i in range(10)]
    fake = FakeStore(rows)
    engine = ReflectionEngine(fake, FakeLLM([]), settings(batch_limit=3))

    report, _ = await engine.sweep(Watermark.beginning(), now=EPOCH)

    # One directory, so one batch -- but only three memories reached it.
    assert report.batches == 1
    assert report.failures == 1  # the empty fake returns None, counted as such


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
