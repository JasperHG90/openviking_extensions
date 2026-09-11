"""The gate that decides what reaches the store.

These are the tests that matter most: everything else can degrade to doing
nothing, but a hole here writes a fabricated claim into memory with a citation
that makes it look sourced.
"""

from __future__ import annotations

import pytest

from ov_ext.reflect.models import CandidateObservation, EvidenceItem
from ov_ext.reflect.verify import (
    normalise,
    areas_covered,
    quote_is_present,
    verify_observations,
)

from .helpers import row

MEMORIES = {
    "viking://user/j/memories/a.md": row(
        "viking://user/j/memories/a.md", "The scheduler retries failed jobs three times."
    ),
    "viking://user/j/memories/b.md": row(
        "viking://user/j/memories/b.md", "Retries use exponential backoff with jitter."
    ),
}
INDEX = {0: "viking://user/j/memories/a.md", 1: "viking://user/j/memories/b.md"}


def observation(*evidence: EvidenceItem) -> CandidateObservation:
    """Build a candidate carrying the given evidence."""
    return CandidateObservation(
        title="retry policy",
        content="Retries are bounded and jittered.",
        evidence=list(evidence),
    )


def cite(index: int | None, quote: str) -> EvidenceItem:
    """Build one evidence item."""
    return EvidenceItem(memory_index=index, quote=quote, relevance_explanation="because")


@pytest.mark.parametrize(
    ("quote", "present"),
    [
        ("retries failed jobs", True),
        ("retries   failed\n  jobs", True),  # whitespace is reflowed by models
        ("Retries failed jobs", False),  # case is content, not formatting
        ("retries failed tasks", False),
        ("", False),
        ("   ", False),
    ],
)
def test_a_quote_counts_only_when_it_is_really_there(quote: str, present: bool) -> None:
    assert (
        quote_is_present(quote, "The scheduler retries failed jobs three times.")
        is present
    )


def test_normalise_leaves_words_alone() -> None:
    assert normalise("  a \n b   c ") == "a b c"


def test_an_observation_whose_quotes_check_out_survives() -> None:
    kept, dropped = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(1, "exponential backoff"))],
        INDEX,
        MEMORIES,
    )
    assert len(kept) == 1
    assert kept[0].evidence == (
        ("viking://user/j/memories/a.md", "retries failed jobs"),
        ("viking://user/j/memories/b.md", "exponential backoff"),
    )
    assert dropped == {
        "bad_index": 0,
        "quote_not_found": 0,
        "too_little_evidence": 0,
        "only_resources": 0,
        "single_area": 0,
    }


def test_an_invented_quote_takes_its_observation_down_with_it() -> None:
    kept, dropped = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(1, "retries seven times"))],
        INDEX,
        MEMORIES,
    )
    assert kept == []
    assert dropped["quote_not_found"] == 1
    assert dropped["too_little_evidence"] == 1


def test_a_citation_outside_the_range_is_a_fabrication() -> None:
    kept, dropped = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(99, "anything"))],
        INDEX,
        MEMORIES,
    )
    assert kept == []
    assert dropped["bad_index"] == 1


def test_a_null_citation_is_a_fabrication_too() -> None:
    _, dropped = verify_observations([observation(cite(None, "x"))], INDEX, MEMORIES)
    assert dropped["bad_index"] == 1


def test_one_quote_is_a_restatement_not_an_observation() -> None:
    kept, dropped = verify_observations(
        [observation(cite(0, "retries failed jobs"))], INDEX, MEMORIES
    )
    assert kept == []
    assert dropped["too_little_evidence"] == 1


def test_the_same_quote_twice_cannot_clear_the_floor_alone() -> None:
    """Two citations of one span are one piece of evidence.

    Without the dedup a model could satisfy `min_evidence` by repeating itself,
    which is exactly the failure the floor exists to catch.
    """
    kept, dropped = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(0, "retries  failed jobs"))],
        INDEX,
        MEMORIES,
    )
    assert kept == []
    assert dropped["too_little_evidence"] == 1


def test_a_lower_floor_lets_a_single_quote_through() -> None:
    kept, _ = verify_observations(
        [observation(cite(0, "retries failed jobs"))], INDEX, MEMORIES, min_evidence=1
    )
    assert len(kept) == 1


def test_spanning_areas_is_recorded_but_not_required_by_default() -> None:
    kept, _ = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(1, "exponential backoff"))],
        INDEX,
        MEMORIES,
    )
    assert kept[0].spans_areas is False


def test_requiring_two_areas_drops_a_single_directory_observation() -> None:
    kept, dropped = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(1, "exponential backoff"))],
        INDEX,
        MEMORIES,
        require_cross_area=True,
    )
    assert kept == []
    assert dropped["single_area"] == 1


def test_requiring_two_areas_keeps_one_that_spans_two_directories() -> None:
    # Two entity categories, which is what a cross-area observation really
    # spans. (Resource URIs would be dropped here for a different reason --
    # see `test_an_observation_resting_only_on_resources_is_dropped`.)
    left = "viking://user/j/memories/entities/software_project/openviking.md"
    right = "viking://user/j/memories/entities/embedding_service/embark.md"
    memories = {
        left: row(left, "openviking coalesces requests."),
        right: row(right, "embedder coalesces requests."),
    }
    index = {0: left, 1: right}
    kept, _ = verify_observations(
        [observation(cite(0, "coalesces requests"), cite(1, "coalesces requests"))],
        index,
        memories,
        require_cross_area=True,
    )
    assert len(kept) == 1
    assert kept[0].spans_areas is True


def test_areas_covered_ignores_uris_it_has_no_row_for() -> None:
    assert areas_covered(["viking://nowhere/x.md"], MEMORIES) == frozenset()


def test_three_quotes_from_one_memory_do_not_clear_a_floor_of_three() -> None:
    """The floor counts distinct memories, not distinct quotes.

    Counting quotes would let nested substrings of a single sentence stand in
    for a synthesis, which is the exact failure the floor exists to reject.
    """
    kept, dropped = verify_observations(
        [
            observation(
                cite(0, "The scheduler"),
                cite(0, "The scheduler retries"),
                cite(0, "retries failed jobs three times"),
            )
        ],
        INDEX,
        MEMORIES,
        min_evidence=3,
    )
    assert kept == []
    assert dropped["too_little_evidence"] == 1


def test_two_distinct_memories_clear_a_floor_of_two() -> None:
    kept, _ = verify_observations(
        [
            observation(
                cite(0, "The scheduler"),
                cite(0, "retries failed jobs"),
                cite(1, "exponential backoff"),
            )
        ],
        INDEX,
        MEMORIES,
        min_evidence=2,
    )
    assert len(kept) == 1
    # Every verified quote is kept as evidence; only the *count* is per memory.
    assert len(kept[0].evidence) == 3
    assert kept[0].sources == {
        "viking://user/j/memories/a.md",
        "viking://user/j/memories/b.md",
    }


def test_an_observation_resting_only_on_resources_is_dropped() -> None:
    """Two chunks of one article can clear the evidence floor between them.

    The result is an observation about the article, filed in the user's memory
    as a finding about them.
    """
    left = "viking://user/j/resources/blog-scraper/nvidia/jetson/part_1.md"
    right = "viking://user/j/resources/blog-scraper/nvidia/jetson/part_2.md"
    memories = {
        left: row(left, "speculative decoding raises throughput."),
        right: row(right, "speculative decoding needs a draft model."),
    }

    kept, dropped = verify_observations(
        [observation(cite(0, "speculative decoding"), cite(1, "speculative decoding"))],
        {0: left, 1: right},
        memories,
    )

    assert kept == []
    assert dropped["only_resources"] == 1


def test_a_resource_may_corroborate_something_the_user_wrote() -> None:
    """Excluded from carrying an observation, not from supporting one."""
    memory = "viking://user/j/memories/entities/dev_tool/ov_ext.md"
    resource = "viking://user/j/resources/blog-scraper/nvidia/jetson.md"
    memories = {
        memory: row(memory, "reflect batches deltas rather than files."),
        resource: row(resource, "batching deltas reduces prompt size."),
    }

    kept, dropped = verify_observations(
        [observation(cite(0, "batches deltas"), cite(1, "batching deltas"))],
        {0: memory, 1: resource},
        memories,
    )

    assert len(kept) == 1
    assert dropped["only_resources"] == 0
