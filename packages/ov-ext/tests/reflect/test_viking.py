"""The adapter, against OpenViking's real classes.

No server and no network: these import the actual OpenViking types and assert
the adapter's assumptions about them. That is the gap that let three wrong API
calls ship — `MemoryFileUtils.serialize` (does not exist),
`MemoryFile(metadata=...)` (silently dropped) and `FindResult.matched_contexts`
(wrong attribute, swallowed by a `getattr` default). Every one would have been
caught by importing the class and looking.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from ov_ext.reflect.exceptions import ContentUnavailableError
from ov_ext.reflect.models import MemoryRow, Observation
from ov_ext.reflect.viking import VikingStore, _digest, _render, _slug

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


class FakeDB:
    """A vector store that returns canned records and remembers the queries."""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = records or []
        self.calls: list[dict[str, Any]] = []

    async def filter(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self._records


class FakeFS:
    """A filesystem that records writes and serves canned reads."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = files or {}
        self.writes: list[tuple[str, str]] = []

    async def read_file(self, uri: str, ctx: Any = None) -> str:
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def write_file(self, uri: str, content: str, ctx: Any = None) -> None:
        self.writes.append((uri, content))
        self.files[uri] = content


class FakeCtx:
    """Just enough request context to name a user."""

    class user:  # noqa: N801 - mirrors OpenViking's attribute shape
        user_id = "jasper"


def store(db: FakeDB | None = None, fs: FakeFS | None = None, **kw: Any) -> VikingStore:
    """Build an adapter over the fakes."""
    return VikingStore(fs or FakeFS(), db or FakeDB(), FakeCtx(), **kw)


def observation() -> Observation:
    """A verified observation with two sources."""
    return Observation(
        title="Both retry",
        content="Both components retry.",
        evidence=(
            ("viking://user/jasper/memories/entities/a.md", "retries failed jobs"),
            ("viking://user/jasper/memories/entities/b.md", "exponential backoff"),
        ),
        areas=frozenset({"viking://user/jasper/memories/entities"}),
    )


# --- the assumptions the adapter makes about OpenViking -------------------


def test_memory_file_utils_has_write_and_not_serialize() -> None:
    """`serialize` was invented; every write raised AttributeError."""
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    assert hasattr(MemoryFileUtils, "write")
    assert hasattr(MemoryFileUtils, "read")
    assert not hasattr(MemoryFileUtils, "serialize")


def test_memory_file_keeps_links_type_and_fields() -> None:
    """`metadata=` is not a field, so Pydantic dropped it and everything in it.

    That silently cost the links, the memory type, and the two fields the
    observations template interpolates into the filename.
    """
    from openviking.session.memory.dataclass import MemoryFile

    memory_file = MemoryFile(
        uri="viking://x/y.md",
        content="body",
        links=[{"from_uri": "a", "to_uri": "b", "link_type": "derived_from"}],
        memory_type="observations",
        extra_fields={"topic": "t", "name": "n"},
    )
    assert memory_file.links and memory_file.memory_type == "observations"
    assert memory_file.extra_fields == {"topic": "t", "name": "n"}
    assert not hasattr(MemoryFile(uri="u"), "metadata")


def test_find_result_exposes_memories_not_matched_contexts() -> None:
    """`matched_contexts` lives on QueryResult; a getattr default hid the miss."""
    from openviking_cli.retrieve.types import FindResult, QueryResult

    assert "memories" in FindResult.__dataclass_fields__
    assert "matched_contexts" not in FindResult.__dataclass_fields__
    assert "matched_contexts" in QueryResult.__dataclass_fields__


def test_matched_context_carries_no_content_or_timestamps() -> None:
    """Which is why neighbours are re-fetched through `rows` rather than used directly.

    Using the context would hand the model an `abstract` to quote and stamp
    every neighbour with the current time.
    """
    from openviking_cli.retrieve.types import MatchedContext

    fields = MatchedContext.__dataclass_fields__
    assert "abstract" in fields
    assert "content" not in fields
    assert "created_at" not in fields


def test_the_filter_expressions_take_the_shapes_used() -> None:
    from openviking.storage.expr import And, Eq, In, PathScope, TimeRange

    And([Eq("context_type", "memory"), Eq("level", 2)])
    In("uri", ["a"])
    PathScope("uri", "viking://user/jasper/memories")
    TimeRange("updated_at", start=NOW)


def test_a_written_observation_round_trips_through_openvikings_own_parser() -> None:
    """The strongest check available without a server: write it, read it back."""
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    fs = FakeFS()
    adapter = store(fs=fs)
    import asyncio

    uri = asyncio.run(adapter.write_observation(observation()))

    written_uri, raw = fs.writes[0]
    assert written_uri == uri
    parsed = MemoryFileUtils.read(raw, uri=uri)
    assert len(parsed.links) == 2
    assert {link["link_type"] for link in parsed.links} == {"derived_from"}
    assert {link["match_text"] for link in parsed.links} == {
        "retries failed jobs",
        "exponential backoff",
    }


# --- behaviour -------------------------------------------------------------


async def test_rows_refuses_when_the_index_holds_no_memory_text() -> None:
    """Falling back to `abstract` would verify quotes against a summary.

    The link written from it would then claim a `match_text` absent from the
    memory it points at — breaking the one OpenViking contract the design
    leans on, invisibly.
    """
    db = FakeDB([{"uri": "viking://x/a.md", "abstract": "a summary", "created_at": NOW}])
    with pytest.raises(ContentUnavailableError, match="store_content"):
        await store(db).rows(["viking://x/a.md"])


async def test_rows_is_quiet_when_there_was_nothing_to_fetch() -> None:
    assert await store(FakeDB([])).rows(["viking://x/a.md"]) == []
    assert await store().rows([]) == []


async def test_the_change_query_scans_oldest_first() -> None:
    """So the batch limit truncates the newest and the rest survive."""
    db = FakeDB([])
    await store(db).changed_since(NOW, limit=5)
    assert db.calls[0]["order_by"] == "updated_at"
    assert db.calls[0]["order_desc"] is False


async def test_the_change_query_asks_for_level_two_only() -> None:
    """L0 and L1 are generated directory summaries whose refresh lags."""
    from openviking.storage.expr import Eq

    db = FakeDB([])
    await store(db).changed_since(NOW, limit=5)
    assert Eq("level", 2) in db.calls[0]["filter"].conds


async def test_the_tail_sample_varies_between_sweeps() -> None:
    """A fixed `ORDER BY oldest LIMIT n` is a constant, and a constant cannot
    break an echo chamber."""
    records = [
        {"uri": f"viking://x/{i}.md", "content": f"note {i}", "created_at": NOW}
        for i in range(30)
    ]
    adapter = store(FakeDB(records), rng=random.Random(1))
    other = store(FakeDB(records), rng=random.Random(2))

    first = [r.uri for r in await adapter.tail_sample(limit=3)]
    second = [r.uri for r in await other.tail_sample(limit=3)]

    assert len(first) == 3
    assert first != second


async def test_the_tail_sample_reads_a_wider_window_than_it_returns() -> None:
    db = FakeDB([])
    await store(db).tail_sample(limit=3)
    assert db.calls[0]["limit"] > 3


async def test_no_tail_sample_asks_the_store_nothing() -> None:
    db = FakeDB([])
    assert await store(db).tail_sample(limit=0) == []
    assert db.calls == []


async def test_a_missing_overview_is_absence_not_failure() -> None:
    """A directory with no overview yet is the normal case."""
    assert await store().read_overview("viking://user/jasper/memories/entities") is None


async def test_a_contradiction_link_is_merged_into_what_the_file_already_has() -> None:
    """The one write touching a file reflection did not author."""
    from openviking.session.memory.dataclass import MemoryFile
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    existing = MemoryFile(
        uri="viking://x/a.md",
        content="body",
        links=[
            {
                "from_uri": "viking://x/a.md",
                "to_uri": "viking://x/keep.md",
                "link_type": "related_to",
                "weight": 0.5,
            }
        ],
    )
    fs = FakeFS({"viking://x/a.md": MemoryFileUtils.write(existing)})

    await store(fs=fs).link(
        "viking://x/a.md", "viking://x/b.md", link_type="contradicts", weight=0.9
    )

    parsed = MemoryFileUtils.read(fs.files["viking://x/a.md"], uri="viking://x/a.md")
    kinds = {link["link_type"] for link in parsed.links}
    assert kinds == {"related_to", "contradicts"}


# --- pure helpers ----------------------------------------------------------


def test_two_observations_with_the_same_title_get_different_files() -> None:
    """Slug alone collides, and write_file would overwrite one with the other."""
    first = observation()
    second = Observation(
        title="Both retry!",
        content="Something else entirely.",
        evidence=(("viking://x/c.md", "other quote"), ("viking://x/d.md", "another")),
        areas=frozenset({"viking://x"}),
    )
    assert _slug(first.title) == _slug(second.title)
    assert _digest(first) != _digest(second)


def test_the_same_conclusion_from_the_same_evidence_keeps_its_filename() -> None:
    """So a re-run merges into the existing file instead of piling up near-duplicates."""
    assert _digest(observation()) == _digest(observation())


def test_the_rendered_body_carries_every_quote() -> None:
    body = _render(observation())
    assert "retries failed jobs" in body
    assert "exponential backoff" in body
    assert body.startswith("# Both retry")


# --- the paths a mutation survived until they were covered ------------------


class FakeFind:
    """What `viking_fs.search` returns: a FindResult-shaped object."""

    def __init__(self, memories: list[Any]) -> None:
        self.memories = memories


class FakeSearchFS(FakeFS):
    """A filesystem whose search returns canned matches."""

    def __init__(self, matches: list[Any]) -> None:
        super().__init__()
        self._matches = matches
        self.searches: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> FakeFind:
        self.searches.append(kwargs)
        return FakeFind(self._matches)


class FakeMatch:
    """A MatchedContext-shaped result: a URI, and no content or timestamps."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.abstract = "a generated summary"


async def test_neighbours_reads_the_attribute_find_result_actually_has() -> None:
    """`matched_contexts` lives on QueryResult; reading it here returns nothing.

    A getattr default hid that, which made the neighbour leg -- the thing that
    makes this reflection rather than summarising the changed window -- a
    silent no-op.
    """
    neighbour = "viking://user/jasper/memories/entities/b.md"
    fs = FakeSearchFS([FakeMatch(neighbour)])
    db = FakeDB([{"uri": neighbour, "content": "older note", "created_at": NOW}])
    subject = MemoryRow(
        uri="viking://x/a.md", text="note", created_at=NOW, updated_at=NOW
    )

    found = await store(db, fs).neighbours(subject, limit=4)

    assert [r.uri for r in found] == [neighbour]
    assert found[0].text == "older note"  # the memory, not the summary


async def test_neighbours_excludes_the_memory_it_started_from() -> None:
    subject = MemoryRow(
        uri="viking://user/jasper/memories/entities/a.md",
        text="note",
        created_at=NOW,
        updated_at=NOW,
    )
    fs = FakeSearchFS([FakeMatch(subject.uri)])
    assert await store(FakeDB([]), fs).neighbours(subject, limit=4) == []


async def test_neighbours_never_cites_a_generated_summary() -> None:
    """A level-suffixed URI names an overview, and quoting one proves nothing."""
    fs = FakeSearchFS([FakeMatch("viking://user/jasper/memories/entities/.overview.md")])
    subject = MemoryRow(
        uri="viking://x/a.md", text="note", created_at=NOW, updated_at=NOW
    )
    assert await store(FakeDB([]), fs).neighbours(subject, limit=4) == []


async def test_the_boundary_row_does_not_come_back_every_sweep() -> None:
    """TimeRange compiles `start` to `>=`, so the row on the mark repeats.

    Harmless once, but with enough rows sharing a timestamp it fills the batch
    and the mark can never advance past them.
    """
    db = FakeDB(
        [
            {"uri": "viking://x/on-the-mark.md", "updated_at": NOW},
            {"uri": "viking://x/after.md", "updated_at": NOW + timedelta(seconds=1)},
        ]
    )
    assert await store(db).changed_since(NOW, limit=10) == ["viking://x/after.md"]


async def test_the_written_uri_carries_the_digest() -> None:
    """Asserting `_digest` in isolation does not prove the filename uses it."""
    fs = FakeFS()
    uri = await store(fs=fs).write_observation(observation())
    assert _digest(observation()) in uri
    assert uri.endswith(".md")


async def test_the_same_memories_are_written_to_the_same_file_twice() -> None:
    """So a re-sweep merges rather than accumulating near-duplicates."""
    fs = FakeFS()
    first = await store(fs=fs).write_observation(observation())
    second = await store(fs=fs).write_observation(observation())
    assert first == second


async def test_different_memories_are_written_to_different_files() -> None:
    other = Observation(
        title="Both retry",
        content="Same title, different sources.",
        evidence=(("viking://x/c.md", "q1"), ("viking://x/d.md", "q2")),
        areas=frozenset({"viking://x"}),
    )
    fs = FakeFS()
    assert await store(fs=fs).write_observation(observation()) != await store(
        fs=fs
    ).write_observation(other)
