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
    peers_covered,
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
        title="retry policy", content="Retries are bounded and jittered.",
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
    assert quote_is_present(quote, "The scheduler retries failed jobs three times.") is present


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
        "single_peer": 0,
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


def test_cross_peer_is_recorded_but_not_required_by_default() -> None:
    kept, _ = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(1, "exponential backoff"))],
        INDEX,
        MEMORIES,
    )
    assert kept[0].is_cross_peer is False


def test_requiring_cross_peer_drops_a_single_project_observation() -> None:
    kept, dropped = verify_observations(
        [observation(cite(0, "retries failed jobs"), cite(1, "exponential backoff"))],
        INDEX,
        MEMORIES,
        require_cross_peer=True,
    )
    assert kept == []
    assert dropped["single_peer"] == 1


def test_requiring_cross_peer_keeps_one_that_spans_two_projects() -> None:
    memories = {
        "viking://user/j/memories/a.md": row(
            "viking://user/j/memories/a.md", "openviking coalesces requests."
        ),
        "viking://user/j/resources/embedder/b.md": row(
            "viking://user/j/resources/embedder/b.md", "embedder coalesces requests."
        ),
    }
    index = {0: "viking://user/j/memories/a.md", 1: "viking://user/j/resources/embedder/b.md"}
    kept, _ = verify_observations(
        [observation(cite(0, "coalesces requests"), cite(1, "coalesces requests"))],
        index,
        memories,
        require_cross_peer=True,
    )
    assert len(kept) == 1
    assert kept[0].is_cross_peer is True


def test_peers_covered_ignores_uris_it_has_no_row_for() -> None:
    assert peers_covered(["viking://nowhere/x.md"], MEMORIES) == frozenset()
