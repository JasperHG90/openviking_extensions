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
from ov_ext.reflect.viking import VikingLLM, VikingStore, _digest, _render, _slug

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


ENTITY = "viking://user/jasper/memories/entities/browser_extension/ov_clip.md"
EVENT = "viking://user/jasper/memories/events/2026/09/07/daily_reflection.md"


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


async def test_a_different_reading_of_the_same_memories_stays_apart() -> None:
    """Sources alone would collapse two genuine claims into one file."""
    first, second = FakeFS(), FakeFS()

    await store(fs=first).write_observation(
        observation_over((ENTITY, "one span"), (EVENT, "another"))
    )
    await store(fs=second).write_observation(
        observation_over((ENTITY, "a different span"), (EVENT, "and another"))
    )

    assert first.writes[0][0] != second.writes[0][0]


async def test_the_filename_says_what_the_observation_is_about() -> None:
    fs = FakeFS()
    await store(fs=fs).write_observation(
        observation_over((ENTITY, "one"), (EVENT, "two"))
    )

    name = fs.writes[0][0].rsplit("/", 1)[-1]
    # Sorted by URI, so the entity sorts before the event.
    assert name.startswith("ov_clip_daily_reflection-"), name


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


async def test_a_resource_chunk_is_named_unambiguously() -> None:
    """Every chunk of every article is `chunk_N`; the stem alone says nothing."""
    from ov_ext.reflect.viking import _label

    chunk = "viking://user/jasper/resources/blog-scraper/nvidia/post.md/intro/chunk_3.md"
    assert _label(chunk) == "resources/blog-scraper/nvidia/post.md/intro/chunk_3"
