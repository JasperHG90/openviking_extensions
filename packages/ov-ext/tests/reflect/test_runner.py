"""Loading the watermark, sweeping, and storing where the sweep got to.

Untested, this is the module that quietly makes every other guarantee moot: if
the mark is never stored, every sweep starts from the beginning of time and
re-reflects the whole store.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ov_ext.reflect.config import ReflectSettings
from ov_ext.reflect.models import Contradictions, ProposedObservations
from ov_ext.reflect.runner import run_sweep
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
    """A model that finds nothing, so a sweep runs end to end and writes none.

    Both shapes, in the order the engine asks for them: observations first,
    then contradictions.
    """
    return FakeLLM(
        [ProposedObservations(observations=[]), Contradictions(relationships=[])] * 4
    )


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
    await run_sweep(fs, FakeDB([]), FakeCtx(), settings(), llm=quiet(), now=NOW)
    # Nothing changed, so nothing to store -- but it did not crash on the
    # missing file, which is the normal first run.
    assert fs.writes == []


async def test_a_sweep_that_advanced_stores_the_new_mark() -> None:
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    await run_sweep(fs, db, FakeCtx(), settings(), llm=quiet(), now=NOW)

    assert [uri for uri, _ in fs.writes if uri == STATE]
    stored = Watermark.loads(fs.files[STATE])
    assert stored.last_seen == datetime(1970, 1, 6, tzinfo=timezone.utc)


async def test_a_sweep_resumes_from_the_stored_mark() -> None:
    """The whole point: the second sweep must not re-read the first one's work."""
    mark = Watermark(last_seen=datetime(1970, 1, 20, tzinfo=timezone.utc), swept_at=NOW)
    fs = FakeFS({STATE: mark.dumps()})
    db = FakeDB([changed_row(5)])  # older than the mark

    report = await run_sweep(fs, db, FakeCtx(), settings(), llm=quiet(), now=NOW)

    assert report.batches == 0
    assert fs.writes == []


async def test_a_dry_run_leaves_the_mark_alone() -> None:
    """Or a dry run would consume the changes it only pretended to process."""
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    await run_sweep(fs, db, FakeCtx(), settings(dry_run=True), llm=quiet(), now=NOW)

    assert fs.writes == []


async def test_a_disabled_sweep_stores_nothing() -> None:
    fs = FakeFS()
    db = FakeDB([changed_row(5)])

    report = await run_sweep(
        fs, db, FakeCtx(), settings(enabled=False), llm=quiet(), now=NOW
    )

    assert report.batches == 0
    assert fs.writes == []


async def test_a_corrupt_mark_costs_one_sweep_not_every_sweep() -> None:
    fs = FakeFS({STATE: "{not json"})
    db = FakeDB([changed_row(5)])

    report = await run_sweep(fs, db, FakeCtx(), settings(), llm=quiet(), now=NOW)

    assert report.batches == 1
    assert Watermark.loads(fs.files[STATE]).last_seen > datetime(
        1970, 1, 1, tzinfo=timezone.utc
    )


async def test_the_stored_mark_is_readable_by_a_person() -> None:
    """It is state an operator will open when a sweep misbehaves."""
    fs = FakeFS()
    await run_sweep(
        fs, FakeDB([changed_row(5)]), FakeCtx(), settings(), llm=quiet(), now=NOW
    )
    parsed = json.loads(fs.files[STATE])
    assert set(parsed) == {"last_seen", "swept_at", "stalls"}
