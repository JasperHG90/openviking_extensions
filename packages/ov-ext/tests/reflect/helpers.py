"""In-memory stand-ins for the two things reflection talks to.

The engine is where the judgement lives, so it has to be runnable without a
server, a database, a model or a network. These satisfy the protocols in
``ov_ext.reflect.ports`` structurally, the same way the real adapters do.

They are stand-ins, not mocks: the store really holds rows and really records
what was written, so a test asserts on what came out rather than on which
method was called.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel

from ov_ext.reflect.models import MemoryRow, Observation

T = TypeVar("T", bound=BaseModel)

EPOCH = datetime(2026, 9, 1, tzinfo=timezone.utc)


def row(uri: str, text: str, *, day: int = 0, updated: int | None = None) -> MemoryRow:
    """Build a memory row at a fixed offset from a fixed date.

    ``updated`` defaults to ``day``; pass it separately to model a memory that
    was created long ago and edited recently, which is the case that
    distinguishes advancing the watermark on the right column from the wrong
    one.
    """
    return MemoryRow(
        uri=uri,
        text=text,
        created_at=EPOCH + timedelta(days=day),
        updated_at=EPOCH + timedelta(days=day if updated is None else updated),
    )


class FakeStore:
    """A memory store that lives in a dict.

    Attributes
    ----------
    written : list[Observation]
        Observations handed to :meth:`write_observation`, in order.
    links : list[tuple[str, str, str, str | None, float]]
        Every edge recorded, as ``(from, to, type, match_text, weight)``.
    """

    def __init__(
        self,
        rows: Sequence[MemoryRow] = (),
        *,
        neighbours: dict[str, list[MemoryRow]] | None = None,
        tail: Sequence[MemoryRow] = (),
        overview: str | None = None,
    ) -> None:
        self._rows = {r.uri: r for r in rows}
        self._neighbours = neighbours or {}
        self._tail = list(tail)
        self._overview = overview
        self.written: list[Observation] = []
        self.links: list[tuple[str, str, str, str | None, float]] = []
        self.reflected: list[str] = []

    async def changed_since(self, moment: datetime, *, limit: int) -> list[str]:
        """Return URIs of rows updated after ``moment``, oldest first.

        Ascending like the real store, so ``limit`` truncates the newest and
        the remainder survives for the next sweep.
        """
        fresh = [r for r in self._rows.values() if r.updated_at > moment]
        fresh.sort(key=lambda r: r.updated_at)
        return [r.uri for r in fresh[:limit]]

    async def rows(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Return the rows for ``uris`` that exist."""
        return [self._rows[uri] for uri in uris if uri in self._rows]

    async def mark_reflected(self, uris: Sequence[str]) -> None:
        """Record which URIs a completed batch retired."""
        self.reflected.extend(uris)

    async def neighbours(self, row: MemoryRow, *, limit: int) -> list[MemoryRow]:
        """Return whatever neighbours were configured for this row."""
        return self._neighbours.get(row.uri, [])[:limit]

    async def tail_sample(self, *, limit: int) -> list[MemoryRow]:
        """Return the configured tail sample."""
        return self._tail[:limit]

    async def read_overview(self, directory: str) -> str | None:
        """Return the configured overview, whatever the directory."""
        return self._overview

    async def write_observation(self, observation: Observation) -> str:
        """Record the observation and hand back a URI for it."""
        self.written.append(observation)
        return f"viking://observations/{len(self.written)}.md"

    async def link(
        self,
        from_uri: str,
        to_uri: str,
        *,
        link_type: str,
        match_text: str | None = None,
        weight: float = 0.5,
    ) -> None:
        """Record the edge."""
        self.links.append((from_uri, to_uri, link_type, match_text, weight))


class FakeLLM:
    """A model that returns whatever it was handed.

    Parameters
    ----------
    replies :
        Queued answers, returned in order. A ``None`` entry stands for a reply
        that did not parse; an ``Exception`` instance is raised, standing for
        the network failing.

    Attributes
    ----------
    prompts : list[str]
        Every prompt received, so a test can assert on what the model was told
        rather than only on what it said.
    """

    def __init__(self, replies: Sequence[Any] = ()) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, prompt: str, model: type[T]) -> T | None:
        """Return the next queued reply, or ``None`` when the queue is empty.

        Raises
        ------
        AssertionError
            If the queued reply is not of the type the caller asked for. The
            engine makes two calls per batch wanting two different shapes, so a
            fake that handed back whichever came next would turn a test's
            queueing mistake into a confusing attribute error deep in the
            engine rather than a clear failure here.
        """
        self.prompts.append(prompt)
        if not self._replies:
            return None
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply is not None and not isinstance(reply, model):
            raise AssertionError(
                f"queued a {type(reply).__name__} but the engine asked for "
                f"{model.__name__}"
            )
        return reply
