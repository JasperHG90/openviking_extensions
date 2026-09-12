"""The adapter, against OpenViking's real classes.

No server and no network: these import the actual OpenViking types and assert
the adapter's assumptions about them. That is the gap that let three wrong API
calls ship — `MemoryFileUtils.serialize` (does not exist),
`MemoryFile(metadata=...)` (silently dropped) and `FindResult.matched_contexts`
(wrong attribute, swallowed by a `getattr` default). Every one would have been
caught by importing the class and looking.
"""

from __future__ import annotations

import copy
import random
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import pytest

from pydantic import BaseModel, Field, PrivateAttr

from ov_ext.reflect.config import ReflectSettings
from ov_ext.reflect.exceptions import ContentUnavailableError
from ov_ext.reflect.models import MemoryRow, Observation
from ov_ext.reflect.viking import VikingLLM, VikingStore, _primary, _render, _subject

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


class FakeDB:
    """A vector store that returns canned records and remembers the queries."""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = records or []
        self.calls: list[dict[str, Any]] = []

    async def filter(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        # Honour an `In("uri", ...)`, because `rows()` relies on it to fetch
        # only what it asked for. A fake that returns everything regardless
        # makes every caller look like it reads the whole store.
        condition = kwargs.get("filter")
        wanted = getattr(condition, "values", None)
        if getattr(condition, "field", None) == "uri" and wanted is not None:
            allowed = set(wanted)
            return [r for r in self._records if r.get("uri") in allowed]
        return self._records


class FakeFS:
    """A filesystem that records writes and serves canned reads."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = files or {}
        self.writes: list[tuple[str, str]] = []

    async def read_file(self, uri: str, ctx: Any = None) -> str:
        # The exception OpenViking's own VikingFS raises for a missing file, not
        # a builtin that looks like it. The store has to tell "no file yet" from
        # "the read failed", and a fake that signalled absence with a different
        # type would let it confuse the two -- which is a write over a file that
        # is still there.
        from openviking_cli.exceptions import NotFoundError

        if uri not in self.files:
            raise NotFoundError(uri)
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
        {
            "uri": f"viking://user/jasper/memories/entities/dev_tool/{i}.md",
            "content": f"note {i}",
            "created_at": NOW,
        }
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


async def test_a_link_is_merged_into_what_the_file_already_has() -> None:
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
        "viking://x/a.md", "viking://x/b.md", link_type="derived_from", weight=0.9
    )

    parsed = MemoryFileUtils.read(fs.files["viking://x/a.md"], uri="viking://x/a.md")
    kinds = {link["link_type"] for link in parsed.links}
    assert kinds == {"related_to", "derived_from"}


# --- pure helpers ----------------------------------------------------------


def test_two_observations_about_different_memories_get_different_files() -> None:
    """The subject names the file, so two subjects cannot land on one."""
    second = Observation(
        title="Both retry!",
        content="Something else entirely.",
        evidence=(("viking://x/c.md", "other quote"), ("viking://x/d.md", "another")),
        areas=frozenset({"viking://x"}),
    )
    assert _subject(observation()) != _subject(second)


def test_the_subject_is_what_the_observation_quotes_most() -> None:
    """Not the first URI sorted: that is a tiebreak, not a subject."""
    about_b = Observation(
        title="About b",
        content="Mostly about b.",
        evidence=(
            ("viking://user/jasper/memories/entities/a.md", "one quote"),
            ("viking://user/jasper/memories/entities/b.md", "first"),
            ("viking://user/jasper/memories/entities/b.md", "second"),
        ),
        areas=frozenset({"viking://user/jasper/memories/entities"}),
    )
    assert _subject(about_b) == "b"
    # The same claim with the counts the other way round follows the quotes.
    assert _subject(observation()) == "a"


def test_a_resource_is_never_what_an_observation_is_filed_under() -> None:
    """Resources are someone else's text; a finding is not about their article."""
    article = "viking://user/jasper/resources/blog-scraper/post.md/chunk_1.md"
    heavily_quoted = Observation(
        title="A claim",
        content="Something is true.",
        evidence=(
            (article, "one"),
            (article, "two"),
            (article, "three"),
            ("viking://user/jasper/memories/entities/tool/embark.md", "just once"),
        ),
        areas=frozenset({"viking://x"}),
    )
    assert _subject(heavily_quoted) == "embark"


def test_an_entity_outranks_a_busier_event() -> None:
    """An observation is about an entity; an event is how it was noticed."""
    event = "viking://user/jasper/memories/events/2026/09/07/standup.md"
    entity = "viking://user/jasper/memories/entities/tool/embark.md"
    mixed = Observation(
        title="A claim",
        content="Something is true.",
        evidence=((event, "one"), (event, "two"), (entity, "just once")),
        areas=frozenset({"viking://x"}),
    )
    assert _primary(mixed) == entity


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
    on_the_mark = "viking://user/jasper/memories/entities/tool/on_the_mark.md"
    after = "viking://user/jasper/memories/entities/tool/after.md"
    db = FakeDB(
        [
            {"uri": on_the_mark, "updated_at": NOW},
            {"uri": after, "updated_at": NOW + timedelta(seconds=1)},
        ]
    )
    assert await store(db).changed_since(NOW, limit=10) == [after]


async def test_the_written_uri_is_the_one_the_engine_was_promised() -> None:
    """The engine reads the standing observation from `observation_uri` first.

    If the write then landed anywhere else, every revision would read an empty
    file and write a new one beside it -- the bug, with an extra step.
    """
    fs = FakeFS()
    adapter = store(fs=fs)
    promised = adapter.observation_uri(observation())
    assert await adapter.write_observation(observation()) == promised
    assert promised.endswith(".md")


async def test_a_write_can_be_pinned_to_the_file_it_is_revising() -> None:
    """Merged evidence can move the subject; the revision must not move with it."""
    fs = FakeFS()
    pinned = "viking://user/jasper/memories/observations/entities/elsewhere.md"

    written = await store(fs=fs).write_observation(observation(), uri=pinned)

    assert written == pinned
    assert fs.writes[0][0] == pinned


async def test_the_same_memories_are_written_to_the_same_file_twice() -> None:
    """So a re-sweep revises rather than accumulating near-duplicates."""
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


# --- what may be drawn on as evidence ---------------------------------------


def evidence_store(**overrides: Any) -> VikingStore:
    """A store with the default evidence scope, unless a test says otherwise."""
    base = {**ReflectSettings().model_dump(), "enabled": True}
    base.update(overrides)
    return store(settings=ReflectSettings.model_construct(**base))


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        ("viking://user/jasper/memories/entities/dev_tool/ov_ext.md", True),
        ("viking://user/jasper/memories/events/2026/09/10/a.md", True),
        ("viking://user/jasper/resources/blog-scraper/nvidia/jetson.md", True),
        # The user's own instructions. An observation resting on one would read
        # them back as a finding.
        ("viking://user/jasper/memories/preferences/jasper/prose_style_rules.md", False),
        ("viking://user/jasper/memories/cases/post-mortem.md", False),
        ("viking://user/jasper/memories/patterns/mem_abc.md", False),
        # Another peer's tree, which this user did not write.
        ("viking://user/jasper/peers/github.com-x/memories/entities/a.md", False),
    ],
)
def test_what_counts_as_evidence(uri: str, allowed: bool) -> None:
    assert evidence_store()._is_evidence(uri) is allowed


def test_resources_are_evidence_only_while_configured() -> None:
    resource = "viking://user/jasper/resources/blog-scraper/nvidia/jetson.md"
    assert evidence_store()._is_evidence(resource) is True
    assert evidence_store(evidence_types=["entities"])._is_evidence(resource) is False


def test_the_evidence_root_reaches_past_memories() -> None:
    """A resource lives outside the memory root, so a search scoped there is blind."""
    store = evidence_store()
    assert store._user_root == "viking://user/jasper"
    assert store._memory_root.startswith(store._user_root)
    assert not "viking://user/jasper/resources/x.md".startswith(store._memory_root)


class SearchingFS(FakeFS):
    """A filesystem whose semantic search returns a fixed set, and records the scope."""

    def __init__(self, uris: list[str]) -> None:
        super().__init__()
        self._uris = uris
        self.searched_under: str | None = None

    async def search(self, **kwargs: Any) -> Any:
        self.searched_under = kwargs.get("target_uri")
        found = [type("Ctx", (), {"uri": uri})() for uri in self._uris]
        return type("Result", (), {"memories": found})()


NEIGHBOURS = [
    "viking://user/jasper/memories/entities/dev_tool/ov_dash.md",
    "viking://user/jasper/memories/preferences/jasper/prose_style_rules.md",
    "viking://user/jasper/resources/blog-scraper/nvidia/jetson.md",
]


async def test_the_neighbour_search_looks_past_the_memory_root() -> None:
    """A resource lives outside `memories/`, so scoping there can never find one."""
    fs = SearchingFS(NEIGHBOURS)
    adapter = store(FakeDB([]), fs)

    await adapter.neighbours(
        MemoryRow(
            uri="viking://user/jasper/memories/entities/a.md",
            text="the scheduler retries",
            created_at=NOW,
            updated_at=NOW,
        ),
        limit=3,
    )

    assert fs.searched_under == "viking://user/jasper"


async def test_neighbours_drop_what_may_not_be_evidence() -> None:
    """A preference returned by search must not reach the model as a neighbour."""
    records = [
        {"uri": uri, "content": f"text of {uri}", "created_at": NOW, "updated_at": NOW}
        for uri in NEIGHBOURS
    ]
    fs = SearchingFS(NEIGHBOURS)
    adapter = store(FakeDB(records), fs)

    found = {
        r.uri
        for r in await adapter.neighbours(
            MemoryRow(
                uri="viking://user/jasper/memories/entities/a.md",
                text="the scheduler retries",
                created_at=NOW,
                updated_at=NOW,
            ),
            limit=3,
        )
    }

    assert "viking://user/jasper/resources/blog-scraper/nvidia/jetson.md" in found
    assert "viking://user/jasper/memories/entities/dev_tool/ov_dash.md" in found
    assert not any("/preferences/" in uri for uri in found)


async def test_the_tail_sample_drops_what_may_not_be_evidence() -> None:
    records = [
        {"uri": uri, "content": f"text of {uri}", "created_at": NOW, "updated_at": NOW}
        for uri in NEIGHBOURS
    ]
    adapter = store(FakeDB(records))

    found = {r.uri for r in await adapter.tail_sample(limit=3)}

    assert not any("/preferences/" in uri for uri in found)
    assert any("/resources/" in uri for uri in found)


# --- how a sweep is batched -------------------------------------------------

BATCH_URIS = [
    "viking://user/jasper/memories/entities/dev_tool/ov_ext.md",
    "viking://user/jasper/memories/entities/dev_tool/prek.md",
    "viking://user/jasper/memories/entities/cli_tool/ovx.md",
    "viking://user/jasper/memories/entities/software_package/ov_postgres.md",
]


class NoDeltas:
    """A delta store shaped enough to switch the store into delta mode."""

    def pending(self, *, limit: int, since: Any = None) -> list[dict[str, Any]]:
        return []

    def mark_reflected(self, ids: Any, *, when: Any) -> int:
        return 0


async def test_a_delta_sweep_is_one_batch() -> None:
    """The measured reason this exists: four directory batches gave 0
    observations where one pooled batch gave 2.

    A delta is a line, not a file, so directory grouping scatters a sweep into
    batches of one or two -- and an observation must cite two distinct memories.
    """
    adapter = store(FakeDB([]), deltas=NoDeltas())

    grouped = adapter.group(BATCH_URIS)

    assert len(grouped) == 1, "a delta sweep must not be split by directory"
    assert sorted(next(iter(grouped.values()))) == sorted(BATCH_URIS)


async def test_a_whole_memory_sweep_is_still_batched_by_directory() -> None:
    """Without deltas nothing changes: a directory's worth is already a prompt."""
    adapter = store(FakeDB([]))

    grouped = adapter.group(BATCH_URIS)

    assert set(grouped) == {
        "viking://user/jasper/memories/entities/dev_tool",
        "viking://user/jasper/memories/entities/cli_tool",
        "viking://user/jasper/memories/entities/software_package",
    }


async def test_a_pooled_batch_has_no_directory_overview() -> None:
    """It spans directories, so no one overview describes it."""
    adapter = store(FakeDB([]), deltas=NoDeltas())
    assert await adapter.read_overview("") is None


async def test_a_large_delta_is_capped_like_any_other_context() -> None:
    """ "A changed memory is delta-sized already" holds for an edit, not a create.

    A created memory records its whole new body as one `replace`, and a dozen of
    those rebuild the prompt the delta work exists to shrink.
    """
    uri = "viking://user/jasper/memories/entities/dev_tool/ov_ext.md"
    huge = "x" * 40_000

    class OneBigDelta:
        def pending(self, *, limit: int, since: Any = None) -> list[dict[str, Any]]:
            return [
                {
                    "id": 1,
                    "uri": uri,
                    "memory_type": "entities",
                    "field": "content",
                    "search": "",
                    "replace": huge,
                    "created": True,
                    "changed_at": NOW,
                }
            ]

        def mark_reflected(self, ids: Any, *, when: Any) -> int:
            return 0

    adapter = store(FakeDB([]), deltas=OneBigDelta())
    await adapter.changed_since(NOW - timedelta(days=1), limit=10)

    rows = await adapter.rows([uri])

    assert len(rows) == 1
    assert len(rows[0].text) == ReflectSettings().context_chars


# --- which model the sweep reasons with -------------------------------------


class FakeCredential(BaseModel):
    """One credential, whose model wins over the config's own."""

    model: str


class FakeVLMConfig(BaseModel):
    """OpenViking's VLMConfig, in the ways that defeated the first attempt.

    A plain stub passes on code that cannot work, so this reproduces all three
    traps the real class set:

    * ``credentials`` carry their own model, and
      ``_build_vlm_config_dict_for_credential`` takes ``credential.model or
      self.model`` -- so setting the top-level name alone changes nothing;
    * the built client is cached on a **pydantic private**, which
      ``object.__setattr__`` cannot reach;
    * that client holds a thread lock, so ``model_copy(deep=True)`` raises.
    """

    model: str
    credentials: list[FakeCredential] = Field(default_factory=list)
    thinking: bool = True
    _vlm_instance: Any = PrivateAttr(default=None)
    _lock: Any = PrivateAttr(default_factory=threading.Lock)

    # Clients built across all instances, so a test can tell one-per-sweep from
    # one-per-call. The real cost is an HTTP client and a handshake.
    builds: ClassVar[int] = 0

    def model_copy(self, *, update: Any = None, deep: bool = False) -> FakeVLMConfig:
        if deep:
            copy.deepcopy(self._lock)  # raises, exactly as pydantic's does
        copied = super().model_copy(update=update or {})
        assert copied.__pydantic_private__ is not None
        copied.__pydantic_private__["_vlm_instance"] = self._vlm_instance
        return copied

    def get_vlm_instance(self) -> Any:
        if self._vlm_instance is None:
            # The credential wins, as it does upstream.
            effective = self.credentials[0].model if self.credentials else self.model
            assert self.__pydantic_private__ is not None
            type(self).builds += 1
            self.__pydantic_private__["_vlm_instance"] = f"instance-for-{effective}"
        return self._vlm_instance

    async def get_completion_async(self, prompt: str = "", thinking: Any = None) -> str:
        """The wrapper that injects ``thinking``.

        A raw client has no such method, which is how a test can tell whether
        the override handed back a config or the thing the config builds.
        """
        effective = self.thinking if thinking is None else thinking
        return f"{self.get_vlm_instance()}|thinking={effective}"


def with_server_model(monkeypatch: pytest.MonkeyPatch, model: str) -> FakeVLMConfig:
    """Point `get_openviking_config().vlm` at a fake carrying `model`."""
    FakeVLMConfig.builds = 0
    config = FakeVLMConfig(model=model, credentials=[FakeCredential(model=model)])
    module = types.ModuleType("openviking_cli.utils.config")
    module.get_openviking_config = lambda: types.SimpleNamespace(vlm=config)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openviking_cli.utils.config", module)
    return config


def reflect_settings(**overrides: Any) -> ReflectSettings:
    base = {**ReflectSettings().model_dump(), "enabled": True}
    base.update(overrides)
    return ReflectSettings.model_construct(**base)


def model_of(config: Any) -> str:
    """The model a reconfigured config will actually build with."""
    chosen = config.credentials[0].model if config.credentials else config.model
    return str(chosen)


def test_the_sweep_uses_its_own_model_when_told_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_server_model(monkeypatch, "ollama/glm-5.3-flash")
    llm = VikingLLM(settings=reflect_settings(model="ollama/deepseek-v4-flash:0731"))

    own = llm._own_vlm("ollama/deepseek-v4-flash:0731")

    assert model_of(own) == "ollama/deepseek-v4-flash:0731"
    assert own.get_vlm_instance() == "instance-for-ollama/deepseek-v4-flash:0731"


def test_the_client_is_built_once_however_many_calls_are_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_get_vlm` runs per request, not per sweep.

    Rebuilding there hands every model call its own HTTP client and a fresh
    handshake -- on a subsystem whose founding symptom was calls timing out.
    """
    config = with_server_model(monkeypatch, "ollama/glm-5.3-flash")
    module = types.ModuleType("openviking_cli.utils.llm")

    class Structured:
        def _get_vlm(self) -> Any:
            return config

    module.StructuredLLM = Structured  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openviking_cli.utils.llm", module)

    llm = VikingLLM(settings=reflect_settings(model="ollama/deepseek-v4-flash:0731"))
    inner = llm._get_llm()
    for _ in range(5):
        inner._get_vlm().get_vlm_instance()

    assert FakeVLMConfig.builds == 1, "one client for the whole sweep, not one per call"


def test_the_override_hands_back_a_config_not_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`StructuredLLM` calls `_get_vlm().get_completion_async(prompt)`.

    The config's wrapper is what injects `thinking`; a raw client's own
    signature defaults it to False, so returning one drops the setting for
    reflection alone.
    """
    with_server_model(monkeypatch, "ollama/glm-5.3-flash")
    llm = VikingLLM(settings=reflect_settings(model="ollama/deepseek-v4-flash:0731"))

    own = llm._own_vlm("ollama/deepseek-v4-flash:0731")

    assert hasattr(own, "get_completion_async"), "must be the config, not the client"


def test_the_override_clears_the_configs_cached_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real VLMConfig caches what it built.

    Copy it without clearing that and `get_vlm_instance()` hands back the model
    the server already built -- the setting reads as applied and does nothing.
    """
    with_server_model(monkeypatch, "ollama/glm-5.3-flash")
    llm = VikingLLM(settings=reflect_settings(model="ollama/deepseek-v4-flash:0731"))

    built = llm._own_vlm("ollama/deepseek-v4-flash:0731")

    assert "glm" not in built


def test_the_override_does_not_disturb_the_servers_own_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything else in the process still uses the model it was configured with."""
    config = with_server_model(monkeypatch, "ollama/glm-5.3-flash")
    llm = VikingLLM(settings=reflect_settings(model="ollama/deepseek-v4-flash:0731"))

    llm._own_vlm("ollama/deepseek-v4-flash:0731")

    assert config.model == "ollama/glm-5.3-flash"
    assert config.get_vlm_instance() == "instance-for-ollama/glm-5.3-flash"


def test_a_model_that_cannot_be_built_falls_back_to_the_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep on the wrong model beats a sweep that cannot start."""
    config = with_server_model(monkeypatch, "ollama/glm-5.3-flash")
    monkeypatch.setattr(
        FakeVLMConfig,
        "model_copy",
        lambda self, deep=False: (_ for _ in ()).throw(RuntimeError("no")),
    )
    llm = VikingLLM(settings=reflect_settings(model="nonsense"))

    # The server's own config, so the sweep keeps its `thinking` and its cache.
    assert llm._own_vlm("nonsense") is config


def test_no_model_configured_leaves_the_server_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must change nothing for a deployment that never set it."""
    calls: list[str] = []
    module = types.ModuleType("openviking_cli.utils.llm")

    class Structured:
        def _get_vlm(self) -> Any:
            calls.append("server")
            return "server-vlm"

    module.StructuredLLM = Structured  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openviking_cli.utils.llm", module)

    VikingLLM(settings=reflect_settings(model=""))._get_llm()._get_vlm()

    assert calls == ["server"]


def test_the_override_is_wired_into_the_llm_the_sweep_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `_get_llm`, which is what the engine actually calls.

    Building the right instance is no use if nothing installs it.
    """
    with_server_model(monkeypatch, "ollama/glm-5.3-flash")
    module = types.ModuleType("openviking_cli.utils.llm")

    class Structured:
        def _get_vlm(self) -> Any:
            return "server-vlm"

    module.StructuredLLM = Structured  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openviking_cli.utils.llm", module)

    llm = VikingLLM(settings=reflect_settings(model="ollama/deepseek-v4-flash:0731"))

    installed = llm._get_llm()._get_vlm()

    assert model_of(installed) == "ollama/deepseek-v4-flash:0731"
    assert installed.get_vlm_instance() == "instance-for-ollama/deepseek-v4-flash:0731"


# --- where an observation is filed, and under what name ---------------------


def observation_over(*evidence: tuple[str, str], title: str = "A claim") -> Observation:
    """An observation citing the given `(uri, quote)` pairs."""
    return Observation(
        title=title,
        content="Something is true.",
        evidence=tuple(evidence),
        areas=frozenset(uri.rsplit("/", 1)[0] for uri, _ in evidence),
    )


MEMORIES = "viking://user/jasper/memories"
ENTITY = f"{MEMORIES}/entities/browser_extension/ov_clip.md"
EVENT = f"{MEMORIES}/events/2026/09/07/daily_reflection.md"


async def test_an_observation_is_filed_under_the_entity_it_is_about() -> None:
    fs = FakeFS()
    await store(fs=fs).write_observation(
        observation_over((ENTITY, "one"), (EVENT, "two"))
    )

    uri = fs.writes[0][0]
    assert "/observations/browser_extension/" in uri


async def test_an_event_never_files_an_observation_under_a_day_number() -> None:
    """`.../events/2026/09/07/x.md` ends in the day of the month.

    Taking the last segment of the area is what gave the store folders called
    06, 07 and 08.
    """
    fs = FakeFS()
    await store(fs=fs).write_observation(
        observation_over((EVENT, "one"), (EVENT.replace("07", "09"), "two"))
    )

    topic = fs.writes[0][0].split("/observations/")[1].split("/")[0]
    assert topic == "events", f"filed under {topic!r}"
    assert not topic.isdigit()


async def test_the_same_claim_reworded_lands_on_the_same_file() -> None:
    """The model does not repeat a title word for word.

    Keeping it in the filename meant the source digest could never collide --
    three files in the live store shared digest 9366b15c with identical
    evidence, one observation written three times.
    """
    first, second = FakeFS(), FakeFS()
    evidence = ((ENTITY, "a quote"), (EVENT, "another quote"))

    await store(fs=first).write_observation(
        observation_over(*evidence, title="Jasper consistently measures token usage")
    )
    await store(fs=second).write_observation(
        observation_over(*evidence, title="Token usage is a recurring concern")
    )

    assert first.writes[0][0] == second.writes[0][0]


async def test_a_quote_reflowed_by_the_model_is_the_same_evidence() -> None:
    """Verification already treats a reflowed quote as the same span."""
    first, second = FakeFS(), FakeFS()

    await store(fs=first).write_observation(
        observation_over((ENTITY, "a quote spanning"), (EVENT, "b"))
    )
    await store(fs=second).write_observation(
        observation_over((ENTITY, "a quote\n  spanning"), (EVENT, "b"))
    )

    assert first.writes[0][0] == second.writes[0][0]


async def test_a_different_reading_of_the_same_memories_lands_on_one_file() -> None:
    """Quoting different spans is not a different subject.

    Hashing the quotes into the filename is what turned one running observation
    about `blog_scraper` into thirteen files: every sweep samples different
    neighbours, so the model quotes different spans and the hash moves even when
    the claim does not.
    """
    first, second = FakeFS(), FakeFS()

    await store(fs=first).write_observation(
        observation_over((ENTITY, "one span"), (EVENT, "another"))
    )
    await store(fs=second).write_observation(
        observation_over((ENTITY, "a different span"), (EVENT, "and another"))
    )

    assert first.writes[0][0] == second.writes[0][0]


async def test_the_filename_says_what_the_observation_is_about() -> None:
    fs = FakeFS()
    await store(fs=fs).write_observation(
        observation_over((ENTITY, "one"), (EVENT, "two"))
    )

    assert fs.writes[0][0].endswith("/observations/browser_extension/ov_clip.md")


async def test_the_body_writes_no_link_of_its_own() -> None:
    """OpenViking linkifies the quote from `match_text`.

    An explicit `[uri](uri)` as well gave two links per bullet: the whole URI
    as its own label, and the quote linkified inside its quotation marks.
    """
    fs = FakeFS()
    await store(fs=fs).write_observation(observation_over((ENTITY, "a quote")))

    body = fs.writes[0][1]
    assert "](viking://" not in body, "the body must not spell out its own links"
    # The path below `memories/`, so two files with the same stem in different
    # categories stay distinguishable.
    assert "- memories/entities/browser_extension/ov_clip: " in body, body


async def test_two_memories_with_the_same_stem_stay_distinguishable() -> None:
    """`ov_clip.md` exists under two categories; naming both `ov_clip` hides which is which."""
    fs = FakeFS()
    other = "viking://user/jasper/memories/entities/software_package/ov_clip.md"
    await store(fs=fs).write_observation(
        observation_over((ENTITY, "first quote"), (other, "second quote"))
    )

    body = fs.writes[0][1]
    assert "- memories/entities/browser_extension/ov_clip: " in body
    assert "- memories/entities/software_package/ov_clip: " in body


async def test_a_quote_that_cannot_be_linked_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`render_links` protects markdown spans and drops the link without a word.

    The bullet still names its source, so provenance survives and only the
    click is lost -- but this store's memories are about code, so backticked
    quotes are common and a silent drop is the kind of thing nobody notices.
    """
    import logging

    fs = FakeFS()
    with caplog.at_level(logging.DEBUG, logger="ov_ext.reflect.viking"):
        await store(fs=fs).write_observation(
            observation_over(
                (ENTITY, "the `--no-verify` flag is never used"),
                (EVENT, "a plain quote"),
            )
        )

    assert "will not render a link" in caplog.text


# --- reading back the observation that already stands ------------------------


async def test_an_observation_round_trips_through_its_own_file() -> None:
    """The revise pass is only as good as what it reads back."""
    fs = FakeFS()
    adapter = store(fs=fs)
    original = observation_over(
        (ENTITY, "one quote"), (EVENT, "another quote"), title="A standing claim"
    )

    uri = await adapter.write_observation(original)
    read_back = await adapter.read_observation(uri)

    assert read_back is not None
    assert read_back.title == "A standing claim"
    assert read_back.content == "Something is true."
    assert read_back.evidence == original.evidence
    assert read_back.areas == original.areas


async def test_the_evidence_read_back_is_the_links_not_the_bullets() -> None:
    """A bullet is rendered text; promoting it to a citation checks nothing.

    The quotes in the links were verified against the memories they name. Prose
    in the file was not, and a file edited by hand would otherwise smuggle a
    citation into the next revision.
    """
    uri = "viking://user/jasper/memories/observations/entities/x.md"
    fs = FakeFS()
    await store(fs=fs).write_observation(observation_over((ENTITY, "real")), uri=uri)
    fs.files[uri] = fs.files[uri].replace(
        "## Evidence", '## Evidence\n\n- invented: "never verified"'
    )

    read_back = await store(fs=fs).read_observation(uri)

    assert read_back is not None
    assert [quote for _, quote in read_back.evidence] == ["real"]


async def test_something_that_is_not_an_observation_is_refused_not_overwritten() -> None:
    """`MemoryFileUtils.read` does not raise on text that is not a memory file.

    It hands the raw bytes back as `content`, so "it parsed" says nothing about
    what is there. Read as an observation, that garbage becomes the standing
    claim the model is asked to revise, and the revision is written back over
    whatever the file really was.
    """
    from ov_ext.reflect.exceptions import ObservationUnreadableError

    uri = "viking://user/jasper/memories/observations/entities/x.md"
    fs = FakeFS({uri: "\x00 not a memory file"})

    with pytest.raises(ObservationUnreadableError, match="derived_from"):
        await store(fs=fs).read_observation(uri)


# --- what the sweep reflects *on* --------------------------------------------


def scoped_paths(condition: Any) -> set[str]:
    """The URI prefixes a change query asked the index for."""
    found: set[str] = set()
    for part in getattr(condition, "conds", []) or []:
        if getattr(part, "path", None) is not None:
            found.add(part.path)
        found |= scoped_paths(part)
    return found


async def test_the_sweep_asks_only_for_the_types_it_reflects_on() -> None:
    """Observations are level-2 memories under the same root as everything else.

    Scoped in the query rather than filtered afterwards. Filtering afterwards
    made `limit` count rows read instead of memories to reflect on, so a burst
    of anything unreflectable -- events, preferences, or the sweep's own
    observations -- filled the window with rows that were all discarded. The
    sweep then reported nothing changed and held the watermark, which is not a
    stall the step-over can clear: the same rows come back every tick, forever.
    """
    db = FakeDB([])

    await store(db).changed_since(NOW, limit=10)

    assert scoped_paths(db.calls[0]["filter"]) == {f"{MEMORIES}/entities"}


async def test_the_scope_follows_the_configured_types() -> None:
    db = FakeDB([])
    adapter = store(db, settings=ReflectSettings(memory_types=["entities", "events"]))

    await adapter.changed_since(NOW, limit=10)

    assert scoped_paths(db.calls[0]["filter"]) == {
        f"{MEMORIES}/entities",
        f"{MEMORIES}/events",
    }


async def test_reflecting_on_no_type_asks_the_index_nothing() -> None:
    """An empty disjunction is as likely to compile to everything as to nothing."""
    db = FakeDB([{"uri": f"{MEMORIES}/entities/tool/e.md", "updated_at": NOW}])
    adapter = store(db, settings=ReflectSettings(memory_types=[]))

    assert await adapter.changed_since(NOW, limit=10) == []
    assert db.calls == [], "no reflectable type means nothing to ask for"


async def test_the_batch_limit_is_what_the_query_asks_for() -> None:
    """The index returns only reflectable rows, so the limit counts memories."""
    db = FakeDB([])
    await store(db).changed_since(NOW, limit=3)
    assert db.calls[0]["limit"] == 3


async def test_a_resource_chunk_is_named_unambiguously() -> None:
    """Every chunk of every article is `chunk_N`; the stem alone says nothing."""
    from ov_ext.reflect.viking import _label

    chunk = "viking://user/jasper/resources/blog-scraper/nvidia/post.md/intro/chunk_3.md"
    assert _label(chunk) == "resources/blog-scraper/nvidia/post.md/intro/chunk_3"


# --- identity: the file follows the memory being reflected on ----------------

CHANGED = f"{MEMORIES}/entities/software_project/blog_scraper.md"


def paired_with(partner: str) -> Observation:
    """An observation about `CHANGED`, drawn beside one neighbour.

    One quote each, which is what `min_evidence=2` produces and therefore the
    modal shape. Both the rank and the count tie, so whatever breaks the tie
    decides the filename.
    """
    return observation_over((CHANGED, "writes captures"), (partner, "a quote"))


async def test_the_partner_an_observation_was_drawn_beside_does_not_name_it() -> None:
    """The bug an evidence-ranked name has, once the hash is gone.

    Five sweeps about `blog_scraper`, each pairing it with a different
    neighbour. Ranking on evidence alone files four of them under the *other*
    entity, because with the counts tied the tiebreak falls through to the
    category folder at the front of the URI -- `cloud_service` before
    `software_project`. Partner-sprawl instead of hash-sprawl.
    """
    partners = [
        f"{MEMORIES}/entities/dev_tool/embark.md",
        f"{MEMORIES}/entities/cloud_service/azure.md",
        f"{MEMORIES}/entities/person/jasper.md",
        f"{MEMORIES}/entities/library/httpx.md",
        f"{MEMORIES}/entities/tool/zed.md",
    ]
    adapter = store()

    landed = {adapter.observation_uri(paired_with(p), {CHANGED}) for p in partners}

    assert len(landed) == 1, f"one subject, {len(landed)} files: {sorted(landed)}"
    assert landed.pop().endswith("/observations/software_project/blog_scraper.md")


async def test_two_subjects_reflected_on_together_keep_their_own_files() -> None:
    """Both changed, so both are subjects; they must not be merged into one."""
    other = f"{MEMORIES}/entities/dev_tool/embark.md"
    adapter = store()
    subjects = {CHANGED, other}

    about_scraper = observation_over((CHANGED, "one"), (CHANGED, "two"), (other, "x"))
    about_embark = observation_over((other, "one"), (other, "two"), (CHANGED, "x"))

    assert adapter.observation_uri(about_scraper, subjects) != adapter.observation_uri(
        about_embark, subjects
    )


async def test_a_subject_that_was_not_reflected_on_still_gets_a_name() -> None:
    """Neighbours-only evidence happens; it must not land on an empty name."""
    uri = store().observation_uri(paired_with(f"{MEMORIES}/entities/dev_tool/e.md"))
    assert uri.rsplit("/", 1)[-1] not in {".md", "untitled.md"}


async def test_two_memories_with_one_filename_do_not_share_an_observation() -> None:
    """Every daily reflection is `daily_reflection.md`; the stem alone collides."""
    adapter = store()
    seventh = observation_over((EVENT, "one"), (EVENT, "two"))
    eighth_uri = EVENT.replace("09/07", "09/08")
    eighth = observation_over((eighth_uri, "one"), (eighth_uri, "two"))

    assert adapter.observation_uri(seventh) != adapter.observation_uri(eighth)


# --- what survives a round trip through OpenViking's own writer --------------


async def round_tripped(obs: Observation) -> Observation:
    """Write an observation, then read it back the way a revision does."""
    fs = FakeFS()
    adapter = store(fs=fs)
    read_back = await adapter.read_observation(await adapter.write_observation(obs))
    assert read_back is not None, "what was just written must be readable"
    return read_back


async def test_a_quote_linkified_into_the_claim_does_not_become_the_claim() -> None:
    """OpenViking linkifies each `match_text` wherever it appears in the body.

    A claim usually restates what it cites, so the stored H1 and paragraph come
    back carrying `[quote](viking://...)`. Read raw, that markup becomes the
    revise prompt's input and then the file's canonical claim, compounding on
    every revision.
    """
    quoted = "retries failed jobs"
    obs = Observation(
        title=f"The scheduler {quoted}",
        content=f"Both components agree that the scheduler {quoted}.",
        evidence=((ENTITY, quoted), (EVENT, "a second span")),
        areas=frozenset({"viking://x"}),
    )

    read_back = await round_tripped(obs)

    assert "](" not in read_back.title, read_back.title
    assert "](" not in read_back.content, read_back.content
    assert read_back.title == obs.title
    assert read_back.content == obs.content


async def test_a_claim_that_mentions_the_evidence_heading_survives() -> None:
    """`_render` writes one Evidence heading and writes it last."""
    obs = Observation(
        title="On rendering",
        content="The body carries a line reading\n\n## Evidence\n\nand then goes on.",
        evidence=((ENTITY, "one"), (EVENT, "two")),
        areas=frozenset({"viking://x"}),
    )

    assert (await round_tripped(obs)).content == obs.content


async def test_a_title_the_model_split_over_two_lines_stays_a_title() -> None:
    """Otherwise the second line joins the claim, one line per revision."""
    obs = Observation(
        title="A title\nwith a second line",
        content="The claim.",
        evidence=((ENTITY, "one"), (EVENT, "two")),
        areas=frozenset({"viking://x"}),
    )

    read_back = await round_tripped(obs)

    assert read_back.title == "A title with a second line"
    assert read_back.content == "The claim."


async def test_an_empty_title_does_not_come_back_as_a_hash() -> None:
    obs = Observation(
        title="",
        content="The claim.",
        evidence=((ENTITY, "one"), (EVENT, "two")),
        areas=frozenset({"viking://x"}),
    )

    read_back = await round_tripped(obs)

    assert read_back.title == "Observation"
    assert read_back.content == "The claim."


# --- absence is not the same as a failed read --------------------------------


async def test_a_read_that_fails_is_raised_not_reported_as_absence() -> None:
    """The caller writes on `None`, so the two must not be spelled the same.

    `VikingFS.read_file` re-raises everything that is not a missing file --
    unreachable, timed out, not permitted -- and reporting any of those as "no
    file yet" overwrites an observation that is still there.
    """
    from openviking_cli.exceptions import UnavailableError

    from ov_ext.reflect.exceptions import ObservationUnreadableError

    class FlakyFS(FakeFS):
        async def read_file(self, uri: str, ctx: Any = None) -> str:
            raise UnavailableError("the index is down")

    with pytest.raises(ObservationUnreadableError):
        await store(fs=FlakyFS()).read_observation("viking://user/jasper/x.md")


async def test_a_file_that_is_simply_absent_is_still_absence() -> None:
    absent = "viking://user/jasper/memories/observations/entities/nothing.md"
    assert await store(fs=FakeFS()).read_observation(absent) is None


async def test_entities_that_differ_only_at_the_end_keep_their_own_files() -> None:
    """A word cap on the name merges them, and the merge is destructive.

    OpenViking's own entity names run long and differ in the last word. Two
    distinct entities sharing a file is not untidy: the revise pass is then told
    they are one subject and asked to fold their claims into one paragraph, and
    there is no other copy to recover from.
    """
    adapter = store()
    pairs = [
        (
            "openviking_memory_plugin_for_claude_code",
            "openviking_memory_plugin_for_claude_desktop",
        ),
        ("ov_clip_browser_extension_for_chrome", "ov_clip_browser_extension_for_firefox"),
    ]
    for first, second in pairs:
        landed = {
            adapter.observation_uri(
                observation_over(
                    (f"{MEMORIES}/entities/tool/{name}.md", "a quote"), (EVENT, "another")
                ),
                {f"{MEMORIES}/entities/tool/{name}.md"},
            )
            for name in (first, second)
        }
        assert len(landed) == 2, f"{first} and {second} share {landed}"


async def test_a_name_too_long_for_a_filesystem_is_still_unique() -> None:
    """Shortened, not truncated: the digest is over the entity's own name.

    Stable in a way the evidence digest this replaced was not -- the name is the
    same every sweep, while the quotes were never the same twice.
    """
    adapter = store()
    stems = [f"a_very_long_entity_name_{'part_' * 20}{tail}" for tail in ("one", "two")]
    landed = [
        adapter.observation_uri(
            observation_over((f"{MEMORIES}/entities/tool/{stem}.md", "q"), (EVENT, "r")),
            {f"{MEMORIES}/entities/tool/{stem}.md"},
        )
        for stem in stems
    ]

    assert landed[0] != landed[1]
    assert all(len(uri.rsplit("/", 1)[-1]) < 120 for uri in landed), landed
    # Same name, same answer, every sweep.
    assert landed[0] == adapter.observation_uri(
        observation_over(
            (f"{MEMORIES}/entities/tool/{stems[0]}.md", "different quote"),
            (EVENT, "and another"),
        ),
        {f"{MEMORIES}/entities/tool/{stems[0]}.md"},
    )


# --- identity survives the subjects alternating ------------------------------


async def test_a_claim_about_two_entities_follows_whichever_one_changed() -> None:
    """The documented cost of filing by subject, asserted so it stays bounded.

    Sweep 1 files under `blog_scraper` because that is what changed; sweep 2
    files the same running claim under `embark` because that is what changed
    then. Two files, each evolving, neither superseding the other.

    Reusing another subject's file to close this was tried twice and regrew a
    magnet both times -- a file's basin is its citation list, which grows with
    every revision, so the most-revised file swallows the first observation
    about every new entity and the revise pass is handed unrelated claims.
    Closing it properly needs a test of whether two claims *say* the same thing,
    which is a model call and a separate feature. Bounded duplication beats
    unbounded destructive merging.
    """
    embark = f"{MEMORIES}/entities/dev_tool/embark.md"
    fs = FakeFS()
    adapter = store(fs=fs)
    claim = observation_over((CHANGED, "writes captures"), (embark, "embeds them"))

    first, standing = await adapter.resolve(claim, {CHANGED})
    assert standing is None
    await adapter.write_observation(claim, uri=first)

    second, found = await adapter.resolve(claim, {embark})

    assert first.endswith("/blog_scraper.md")
    assert second.endswith("/embark.md")
    assert found is None


async def test_a_file_is_not_reused_just_because_it_cites_the_same_memory() -> None:
    """Sharing a citation is not sharing a subject.

    Otherwise the busiest memory in the store -- in a personal store, the user's
    own person entity, which half of everything cites -- becomes a magnet. The
    first observation about any new entity is swallowed into it, so that
    entity's file is never created and every later observation about it is
    swallowed too. The revise pass is then handed unrelated claims with the
    instruction to fold them into one, and nothing keeps a second copy.
    """
    jasper = f"{MEMORIES}/entities/person/jasper.md"
    fs = FakeFS()
    adapter = store(fs=fs)

    about_zed = observation_over(
        (jasper, "prefers a terminal"),
        (f"{MEMORIES}/entities/tool/zed.md", "is an editor"),
    )
    first, _ = await adapter.resolve(about_zed, {jasper})
    await adapter.write_observation(about_zed, uri=first)

    # A different claim, about a different entity, that happens to cite jasper.
    about_scraper = observation_over((CHANGED, "writes captures"), (jasper, "runs it"))
    landed, standing = await adapter.resolve(about_scraper, {CHANGED})

    assert standing is None, "an unrelated claim must not be folded into this one"
    assert landed != first
    assert landed.endswith("/blog_scraper.md")


async def test_a_much_revised_file_does_not_swallow_a_new_entity() -> None:
    """The magnet, regrown through an accumulated citation list.

    A file's citations grow with every revision, so a rule keyed on them gives
    the busiest, most-revised file the widest reach. Here `jasper.md` has quite
    legitimately come to cite `blog_scraper` -- and the first observation ever
    written about `blog_scraper` must still get its own file.
    """
    jasper = f"{MEMORIES}/entities/person/jasper.md"
    fs = FakeFS()
    adapter = store(fs=fs)

    accumulated = observation_over(
        (jasper, "prefers a terminal"),
        (f"{MEMORIES}/entities/tool/zed.md", "is an editor"),
        (CHANGED, "was written by him"),
    )
    magnet, _ = await adapter.resolve(accumulated, {jasper})
    await adapter.write_observation(accumulated, uri=magnet)
    assert magnet.endswith("/jasper.md")

    landed, standing = await adapter.resolve(
        observation_over((CHANGED, "retries with backoff"), (jasper, "runs it")),
        {CHANGED},
    )

    assert standing is None, "an unrelated claim must not be folded into this one"
    assert landed.endswith("/blog_scraper.md")


async def test_a_genuinely_new_subject_still_gets_its_own_file() -> None:
    """Stickiness must not collapse every observation onto the first file made."""
    embark = f"{MEMORIES}/entities/dev_tool/embark.md"
    other = f"{MEMORIES}/entities/library/httpx.md"
    fs = FakeFS()
    adapter = store(fs=fs)

    scraper = observation_over((CHANGED, "one"), (embark, "two"))
    scraper_uri, _ = await adapter.resolve(scraper, {CHANGED})
    await adapter.write_observation(scraper, uri=scraper_uri)

    # Overlapping citation on purpose: with no overlap no candidate could hit,
    # and the test would prove only that unrelated files stay apart -- which
    # was never the risk.
    fresh, standing = await adapter.resolve(
        observation_over((other, "three"), (embark, "four")), {other}
    )

    assert standing is None
    assert fresh != scraper_uri
    assert fresh.endswith("/httpx.md")


async def test_a_subjects_own_file_is_found_even_if_it_stopped_citing_it() -> None:
    """Evidence rotates out under the cap; the file is still that subject's.

    Without this the sweep would skip the file named after the subject, find
    nothing, and write a fresh observation straight over it -- losing every
    revision it had accumulated, precisely because it had accumulated enough for
    the founding quotes to age out.
    """
    fs = FakeFS()
    adapter = store(fs=fs)
    embark = f"{MEMORIES}/entities/dev_tool/embark.md"

    own_uri = adapter.observation_uri(paired_with(embark), {CHANGED})
    # What stands there no longer quotes blog_scraper at all.
    aged = observation_over((embark, "one"), (EVENT, "two"))
    await adapter.write_observation(aged, uri=own_uri)

    landed, standing = await adapter.resolve(paired_with(embark), {CHANGED})

    assert landed == own_uri
    assert standing is not None, "its own file must be found, cited or not"


async def test_an_unreadable_candidate_stops_the_search_rather_than_stepping_past() -> (
    None
):
    """Falling through would write a second file for a subject that may have one."""
    from openviking_cli.exceptions import UnavailableError

    from ov_ext.reflect.exceptions import ObservationUnreadableError

    class FlakyFS(FakeFS):
        async def read_file(self, uri: str, ctx: Any = None) -> str:
            raise UnavailableError("the index is down")

    with pytest.raises(ObservationUnreadableError):
        await store(fs=FlakyFS()).resolve(paired_with(f"{MEMORIES}/entities/t/e.md"))


async def test_a_watermark_plateau_that_wedges_the_sweep_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`TimeRange` compiles `start` to `>=`, so rows on the mark repeat.

    One is harmless. A full page of them is a sweep that reads the same rows
    every tick, reports nothing changed, and holds the watermark with no stall
    counted -- invisible unless it says so.
    """
    import logging

    db = FakeDB(
        [
            {"uri": f"{MEMORIES}/entities/tool/e{n}.md", "updated_at": NOW}
            for n in range(3)
        ]
    )

    with caplog.at_level(logging.ERROR, logger="ov_ext.reflect.viking"):
        assert await store(db).changed_since(NOW, limit=3) == []

    assert "cannot see past them" in caplog.text
    assert "OV_REFLECT_BATCH_LIMIT" in caplog.text


async def test_a_partial_page_on_the_mark_is_not_a_plateau(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ordinary boundary row must not cry wolf every sweep."""
    import logging

    db = FakeDB([{"uri": f"{MEMORIES}/entities/tool/e.md", "updated_at": NOW}])

    with caplog.at_level(logging.ERROR, logger="ov_ext.reflect.viking"):
        assert await store(db).changed_since(NOW, limit=10) == []

    assert caplog.text == ""


async def test_the_alternation_is_per_co_cited_entity_not_per_pair() -> None:
    """State the accepted cost at its real size, so a reader is not surprised.

    Someone finding one claim in four entity files in six months should be able
    to confirm here that it is the design rather than a regression.
    """
    partners = [
        f"{MEMORIES}/entities/dev_tool/embark.md",
        f"{MEMORIES}/entities/library/httpx.md",
        f"{MEMORIES}/entities/tool/zed.md",
    ]
    adapter = store()
    claim = observation_over(
        (CHANGED, "writes captures"), *((p, "a quote") for p in partners)
    )

    landed = {adapter.observation_uri(claim, {who}) for who in [CHANGED, *partners]}

    assert len(landed) == 4, "one file per entity that is itself reflected on"
    assert all(uri.endswith(".md") for uri in landed)
