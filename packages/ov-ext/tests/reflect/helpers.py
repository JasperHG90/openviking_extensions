"""In-memory stand-ins for the two things reflection talks to.

The engine is where the judgement lives, so it has to be runnable without a
server, a database, a model or a network. These satisfy the protocols in
``ov_ext.reflect.ports`` structurally, the same way the real adapters do.

They are stand-ins, not mocks: the store really holds rows and really records
what was written, so a test asserts on what came out rather than on which
method was called.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
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
    standing : dict[str, Observation]
        What each file holds now, so a second write to one subject finds the
        first -- which is the whole behaviour the revise pass turns on.
    unreadable : dict[str, Exception]
        URIs whose next read raises, for modelling a store that is reachable
        but will not answer.
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
        self.standing: dict[str, Observation] = {}
        self.unreadable: dict[str, Exception] = {}
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

    @property
    def reads_deltas(self) -> bool:
        """This fake serves whole memories, as the real store does without deltas."""
        return False

    def group(self, uris: Sequence[str]) -> dict[str, list[str]]:
        """Batch by directory, like the store reading whole memories."""
        from ov_ext.reflect.engine import group_by_directory

        return group_by_directory(uris)

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

    def observation_uri(
        self, observation: Observation, subjects: Collection[str] = ()
    ) -> str:
        """Name the file the way the real store does.

        The real helpers, not a second copy of the rule: what the engine relies
        on is that one subject resolves to one file, and a fake that numbered
        its writes instead would satisfy every revision test while the live
        store kept creating new files.
        """
        from ov_ext.reflect.viking import _subject, _topic

        topic = _topic(observation, subjects)
        return f"viking://observations/{topic}/{_subject(observation, subjects)}.md"

    async def read_observation(self, uri: str) -> Observation | None:
        """Return what was last written to ``uri``, or ``None``.

        Raises whatever ``unreadable`` holds for that URI, so a test can model a
        store that is up but will not answer -- which the engine has to tell
        apart from a file that is not there. The entry is NOT consumed: a file
        that fails once and then reads fine cannot show what a persistently
        stuck subject does to the sweeps behind it.
        """
        problem = self.unreadable.get(uri)
        if problem is not None:
            raise problem
        return self.standing.get(uri)

    async def resolve(
        self, observation: Observation, subjects: Collection[str] = ()
    ) -> tuple[str, Observation | None]:
        """Prefer a file that exists over a better-ranked one that does not.

        The real rule, not a shortcut, including the part that keeps it from
        becoming a magnet: a runner-up's file is reused only when what stands
        there already cites the memory this observation is about.
        """
        from ov_ext.reflect.viking import _ranked

        ranked = _ranked(observation, subjects)
        if not ranked:
            return self.observation_uri(observation, subjects), None
        subject = ranked[0]
        candidates = [self.observation_uri(observation, [uri]) for uri in ranked[:3]]
        for candidate in dict.fromkeys(candidates):
            standing = await self.read_observation(candidate)
            if standing is None:
                continue
            if candidate == candidates[0] or subject in standing.sources:
                return candidate, standing
        return candidates[0], None

    async def whole_memories(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Return whole memories, which is all this fake ever held."""
        return await FakeStore.rows(self, uris)

    async def write_observation(
        self, observation: Observation, *, uri: str | None = None
    ) -> str:
        """Record the observation and hand back the URI it landed on."""
        uri = uri or self.observation_uri(observation)
        self.written.append(observation)
        self.standing[uri] = observation
        return uri

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
