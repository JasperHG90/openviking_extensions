"""Loading the watermark, sweeping, and storing where the sweep got to.

Untested, this is the module that quietly makes every other guarantee moot: if
the mark is never stored, every sweep starts from the beginning of time and
re-reflects the whole store.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from ov_ext.reflect.config import ReflectSettings
from ov_ext.reflect.models import ProposedObservations
from ov_ext.reflect.locks import ProcessLock
from ov_ext.reflect.runner import run_sweep
from ov_ext.reflect.verify import quote_is_present
from ov_ext.reflect.viking import VikingStore
from ov_ext.reflect.watermark import Watermark

from .helpers import FakeLLM

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
STATE = "viking://user/jasper/resources/reflect/watermark.json"


class FakeFS:
    """A filesystem that serves canned reads and records writes."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})
        self.writes: list[tuple[str, str]] = []

    async def read_file(self, uri: str, ctx: Any = None) -> str:
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def write_file(self, uri: str, content: str, ctx: Any = None) -> None:
        self.writes.append((uri, content))
        self.files[uri] = content

    async def search(self, **kwargs: Any) -> Any:
        raise AssertionError("no sweep in these tests should reach search")


class FakeDB:
    """A vector store holding one changed memory, or none."""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = records or []

    async def filter(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._records


class FakeCtx:
    class user:  # noqa: N801 - mirrors OpenViking's attribute shape
        user_id = "jasper"


def settings(**overrides: Any) -> ReflectSettings:
    """Settings with reflection on and the environment ignored."""
    base = ReflectSettings().model_dump()
    base.update({"enabled": True, "neighbour_limit": 0, "tail_sample": 0})
    base.update(overrides)
    return ReflectSettings.model_construct(**base)


def quiet() -> FakeLLM:
    """A model that finds nothing, so a sweep runs end to end and writes none."""
    return FakeLLM([ProposedObservations(observations=[])] * 8)


def changed_row(day: int) -> dict[str, Any]:
    """One indexed memory row, changed `day` days after the epoch."""
    when = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    return {
        "uri": "viking://user/jasper/memories/entities/a.md",
        "content": "The scheduler retries failed jobs.",
        "created_at": when,
        "updated_at": when,
    }


async def test_a_store_never_swept_starts_from_the_beginning() -> None:
    fs = FakeFS()
    await run_sweep(
        fs, FakeDB([]), FakeCtx(), settings(), lock=ProcessLock(), llm=quiet(), now=NOW
    )
    # Nothing changed, so nothing to store -- but it did not crash on the
    # missing file, which is the normal first run.
    assert fs.writes == []


async def test_a_sweep_that_advanced_stores_the_new_mark() -> None:
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    await run_sweep(
        fs, db, FakeCtx(), settings(), lock=ProcessLock(), llm=quiet(), now=NOW
    )

    assert [uri for uri, _ in fs.writes if uri == STATE]
    stored = Watermark.loads(fs.files[STATE])
    assert stored.last_seen == datetime(1970, 1, 6, tzinfo=timezone.utc)


async def test_a_sweep_resumes_from_the_stored_mark() -> None:
    """The whole point: the second sweep must not re-read the first one's work."""
    mark = Watermark(last_seen=datetime(1970, 1, 20, tzinfo=timezone.utc), swept_at=NOW)
    fs = FakeFS({STATE: mark.dumps()})
    db = FakeDB([changed_row(5)])  # older than the mark

    report = await run_sweep(
        fs, db, FakeCtx(), settings(), lock=ProcessLock(), llm=quiet(), now=NOW
    )

    assert report.batches == 0
    assert fs.writes == []


async def test_a_dry_run_leaves_the_mark_alone() -> None:
    """Or a dry run would consume the changes it only pretended to process."""
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    await run_sweep(
        fs,
        db,
        FakeCtx(),
        settings(dry_run=True),
        lock=ProcessLock(),
        llm=quiet(),
        now=NOW,
    )

    assert fs.writes == []


async def test_a_disabled_sweep_stores_nothing() -> None:
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    report = await run_sweep(
        fs,
        db,
        FakeCtx(),
        settings(enabled=False),
        lock=ProcessLock(),
        llm=quiet(),
        now=NOW,
    )

    assert report.batches == 0
    assert fs.writes == []


async def test_a_corrupt_mark_costs_one_sweep_not_every_sweep() -> None:
    fs = FakeFS({STATE: "{not json"})
    db = FakeDB([changed_row(5)])

    report = await run_sweep(
        fs, db, FakeCtx(), settings(), lock=ProcessLock(), llm=quiet(), now=NOW
    )

    assert report.batches == 1
    assert Watermark.loads(fs.files[STATE]).last_seen > datetime(
        1970, 1, 1, tzinfo=timezone.utc
    )


async def test_the_stored_mark_is_readable_by_a_person() -> None:
    """It is state an operator will open when a sweep misbehaves."""
    fs = FakeFS()
    await run_sweep(
        fs,
        FakeDB([changed_row(5)]),
        FakeCtx(),
        settings(),
        lock=ProcessLock(),
        llm=quiet(),
        now=NOW,
    )
    parsed = json.loads(fs.files[STATE])
    assert set(parsed) == {"last_seen", "swept_at", "stalls"}


class BrokenLLM:
    """A model whose every call fails, standing in for a provider outage."""

    async def complete(self, prompt: str, model: Any) -> Any:
        raise RuntimeError("provider unreachable")


async def test_a_stalled_sweep_stores_the_stall_it_counted() -> None:
    """Otherwise `max_stalls` is dead code and a failing batch blocks forever.

    A sweep that reads something and cannot advance leaves `last_seen` where it
    was and raises `stalls`. Storing only on a moved `last_seen` discards that
    count, so every sweep reloads stalls=0, the step-over never triggers, and
    the same batch is retried until someone notices by hand.
    """
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    report = await run_sweep(
        fs, db, FakeCtx(), settings(), lock=ProcessLock(), llm=BrokenLLM(), now=NOW
    )

    assert report.failures  # the batch really did fail
    stored = Watermark.loads(fs.files[STATE])
    assert stored.last_seen == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert stored.stalls == 1


async def test_stalls_accumulate_until_the_sweep_steps_over_the_blockage() -> None:
    """The escape hatch, exercised end to end through the stored mark.

    Each sweep has to read the previous one's stall count for the limit to mean
    anything, so this runs four sweeps against the same filesystem rather than
    handing the engine a watermark directly.
    """
    fs = FakeFS()
    db = FakeDB([changed_row(5)])
    config = settings(max_stalls=3)

    for _ in range(3):
        await run_sweep(
            fs, db, FakeCtx(), config, lock=ProcessLock(), llm=BrokenLLM(), now=NOW
        )
    assert Watermark.loads(fs.files[STATE]).stalls == 3

    report = await run_sweep(
        fs, db, FakeCtx(), config, lock=ProcessLock(), llm=BrokenLLM(), now=NOW
    )

    assert report.stepped_over
    stored = Watermark.loads(fs.files[STATE])
    assert stored.last_seen == datetime(1970, 1, 6, tzinfo=timezone.utc)
    assert stored.stalls == 0


async def test_a_sweep_that_read_nothing_still_writes_nothing() -> None:
    """The mark is state in someone's store; an idle sweep must not churn it."""
    mark = Watermark(last_seen=datetime(1970, 1, 20, tzinfo=timezone.utc), swept_at=NOW)
    fs = FakeFS({STATE: mark.dumps()})

    await run_sweep(
        fs, FakeDB([]), FakeCtx(), settings(), lock=ProcessLock(), llm=quiet(), now=NOW
    )

    assert fs.writes == []


class NeverLock:
    """A lock nobody can take, standing in for another process holding it."""

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        yield False


async def test_a_sweep_that_cannot_take_the_lock_does_nothing_at_all() -> None:
    """The guarantee at the only entry point there is.

    run_sweep is exported and documented for scripts, so it -- not the ticker
    -- is where the lock has to be taken. A caller who cannot get it must read
    nothing, write nothing, and leave the mark alone.
    """
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    report = await run_sweep(
        fs, db, FakeCtx(), settings(), lock=NeverLock(), llm=quiet(), now=NOW
    )

    assert report.batches == 0
    assert report.written == 0
    assert fs.writes == []


async def test_the_lock_is_required_not_optional() -> None:
    """A keyword-only required argument: there is no way to forget it."""
    with pytest.raises(TypeError):
        await run_sweep(  # type: ignore[call-arg]
            FakeFS(), FakeDB([]), FakeCtx(), settings(), llm=quiet(), now=NOW
        )


class FakeDeltas:
    """A delta store holding what capture would have recorded."""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.records = records or []
        self.retired: list[int] = []

    def pending(self, *, limit: int, since: Any = None) -> list[dict[str, Any]]:
        return [r for r in self.records if r["id"] not in self.retired][:limit]

    def mark_reflected(self, ids: Any, *, when: datetime) -> int:
        self.retired.extend(ids)
        return len(list(ids))


def delta_row(row_id: int, uri: str, replace: str, day: int = 1) -> dict[str, Any]:
    """One captured change, as the store hands it back."""
    return {
        "id": row_id,
        "uri": uri,
        "memory_type": "entities",
        "field": "content",
        "search": "",
        "replace": replace,
        "created": False,
        "changed_at": datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
    }


async def test_a_sweep_reads_the_change_not_the_whole_memory() -> None:
    """The point of the delta work, end to end through run_sweep.

    The vector store here returns nothing at all. If the sweep still reached for
    whole memories it would find none and do nothing; the batch it runs can only
    have come from the delta store.
    """
    fs = FakeFS()
    uri = "viking://user/jasper/memories/entities/a.md"
    deltas = FakeDeltas(
        [delta_row(1, uri, "- ships v0.4"), delta_row(2, uri, "- adds MMR")]
    )

    report = await run_sweep(
        fs,
        FakeDB([]),
        FakeCtx(),
        settings(),
        lock=ProcessLock(),
        llm=quiet(),
        now=NOW,
        deltas=deltas,
    )

    assert report.batches == 1, "the batch came from the delta store"


async def test_a_completed_batch_retires_the_deltas_it_read() -> None:
    """Otherwise every sweep re-reads the same changes forever."""
    uri = "viking://user/jasper/memories/entities/a.md"
    deltas = FakeDeltas([delta_row(1, uri, "- ships v0.4")])

    await run_sweep(
        FakeFS(),
        FakeDB([]),
        FakeCtx(),
        settings(),
        lock=ProcessLock(),
        llm=quiet(),
        now=NOW,
        deltas=deltas,
    )

    assert deltas.retired == [1]
    assert deltas.pending(limit=10) == []


async def test_a_failed_batch_leaves_its_deltas_pending() -> None:
    """A delta retired by a sweep that failed is a change nothing reflects on."""
    uri = "viking://user/jasper/memories/entities/a.md"
    deltas = FakeDeltas([delta_row(1, uri, "- ships v0.4")])

    await run_sweep(
        FakeFS(),
        FakeDB([]),
        FakeCtx(),
        settings(),
        lock=ProcessLock(),
        llm=BrokenLLM(),
        now=NOW,
        deltas=deltas,
    )

    assert deltas.retired == []
    assert len(deltas.pending(limit=10)) == 1


def deletion_row(row_id: int, uri: str, removed: str, day: int = 1) -> dict[str, Any]:
    """A captured deletion: text went away and nothing replaced it."""
    row = delta_row(row_id, uri, "", day=day)
    row["search"] = removed
    return row


async def test_deleted_text_is_never_shown_to_the_model() -> None:
    """Citing a retracted line would produce a link the store cannot render.

    Verification would pass -- the quote really is in the row the model was
    shown -- and the `derived_from` link's `match_text` would then be absent
    from the memory it points at. OpenViking renders a link by finding that
    span, so the observation would rest on evidence nobody can see.
    """
    uri = "viking://user/jasper/memories/entities/a.md"
    deltas = FakeDeltas([deletion_row(1, uri, "- a claim that turned out wrong")])
    store = VikingStore(FakeFS(), FakeDB([]), FakeCtx(), settings(), deltas=deltas)

    await store.changed_since(datetime(1970, 1, 1, tzinfo=timezone.utc), limit=10)
    rows = await store.rows([uri])

    assert rows == [], "a pure deletion leaves nothing citable"


async def test_a_batch_of_only_deletions_does_not_stall_the_sweep() -> None:
    """It read them; leaving them pending would re-read them every tick forever."""
    uri = "viking://user/jasper/memories/entities/a.md"
    deltas = FakeDeltas([deletion_row(1, uri, "- a claim that turned out wrong")])

    await run_sweep(
        FakeFS(),
        FakeDB([]),
        FakeCtx(),
        settings(),
        lock=ProcessLock(),
        llm=quiet(),
        now=NOW,
        deltas=deltas,
    )

    assert deltas.retired == [1]
    assert deltas.pending(limit=10) == []


async def test_an_edit_shows_the_new_wording_not_the_old() -> None:
    """The replace side is what the memory says now, so it is what can be cited."""
    uri = "viking://user/jasper/memories/entities/a.md"
    record = delta_row(1, uri, "- rerank caps at 6 calls")
    record["search"] = "- rerank caps at 3 calls"
    deltas = FakeDeltas([record])
    store = VikingStore(FakeFS(), FakeDB([]), FakeCtx(), settings(), deltas=deltas)

    await store.changed_since(datetime(1970, 1, 1, tzinfo=timezone.utc), limit=10)
    rows = await store.rows([uri])

    assert rows[0].text == "- rerank caps at 6 calls"
    assert "3 calls" not in rows[0].text


async def test_a_neighbour_without_deltas_is_not_lost_to_one_that_has_them() -> None:
    """Per URI, not per call.

    Resolving the whole call from deltas as soon as any URI has them takes every
    other memory out of the evidence pool -- which starves `min_evidence` while
    the sweep still looks healthy.
    """
    changed = "viking://user/jasper/memories/entities/a.md"
    plain = "viking://user/jasper/memories/entities/b.md"
    deltas = FakeDeltas([delta_row(1, changed, "- ships v0.4")])
    db = FakeDB(
        [
            {
                "uri": plain,
                "content": "The worker retries failed jobs.",
                "created_at": NOW,
                "updated_at": NOW,
            }
        ]
    )
    store = VikingStore(FakeFS(), db, FakeCtx(), settings(), deltas=deltas)

    await store.changed_since(datetime(1970, 1, 1, tzinfo=timezone.utc), limit=10)
    rows = await store.rows([changed, plain])

    assert {row.uri for row in rows} == {changed, plain}
    assert rows[0].uri == changed, "the changed memory keeps the low citation index"


async def test_a_quote_cannot_span_two_unrelated_changes() -> None:
    """Verification collapses whitespace, so a newline join makes them adjacent.

    Two edits made at opposite ends of a 20 KB file would become neighbouring
    lines, and a quote running across both would verify against a span that
    exists in no memory.
    """
    uri = "viking://user/jasper/memories/entities/a.md"
    deltas = FakeDeltas(
        [
            delta_row(1, uri, "ov-ext ships v0.4"),
            delta_row(2, uri, "ov-dash uses Svelte"),
        ]
    )
    store = VikingStore(FakeFS(), FakeDB([]), FakeCtx(), settings(), deltas=deltas)

    await store.changed_since(datetime(1970, 1, 1, tzinfo=timezone.utc), limit=10)
    row = (await store.rows([uri]))[0]

    assert quote_is_present("ov-ext ships v0.4", row.text)
    assert quote_is_present("ov-dash uses Svelte", row.text)
    assert not quote_is_present("ov-ext ships v0.4 ov-dash uses Svelte", row.text)


async def test_a_read_does_not_serve_text_the_previous_read_loaded() -> None:
    """`_delta_text` must be reset with `_consumed`, not left to accumulate.

    A batch that failed never retires its URIs, so nothing pops them. The next
    read would then serve whatever the last one loaded -- for a memory the store
    no longer reports as changed at all.
    """
    gone = "viking://user/jasper/memories/entities/a.md"
    fresh = "viking://user/jasper/memories/entities/b.md"
    deltas = FakeDeltas([delta_row(1, gone, "text from the first read")])
    store = VikingStore(FakeFS(), FakeDB([]), FakeCtx(), settings(), deltas=deltas)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    await store.changed_since(epoch, limit=10)

    # The first URI's deltas go away out of band; a different memory changes.
    deltas.records = [delta_row(2, fresh, "text from the second read")]
    await store.changed_since(epoch, limit=10)

    assert await store.rows([gone]) == [], "the first read's text must not survive"
