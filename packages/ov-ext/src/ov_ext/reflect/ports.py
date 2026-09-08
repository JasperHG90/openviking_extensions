"""What reflection needs from the world, stated as narrowly as possible.

Two protocols, deliberately small. The engine is where the judgement lives and
where the bugs will be, so it has to be runnable against fakes -- without a
server, a database, a model, or a network. Everything that touches those sits
behind one of these and is implemented once, in :mod:`ov_ext.reflect.viking`.

Structural typing rather than base classes: OpenViking knows nothing about
this package and never will, so the adapter satisfies the shape without
inheriting anything, and the dependency points one way.

These are *not* an extension seam. There is one implementation of each and no
plan for a second -- OpenViking is the only store there is. Widening them into
a plugin interface would buy optionality nobody will spend.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TypeVar

from pydantic import BaseModel

from .models import MemoryRow, Observation

__all__ = ["MemoryStore", "StructuredLLM"]

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    """A model that answers in a given shape, or does not answer.

    Matches ``StructuredVLM.complete_model``: OpenViking appends the JSON
    schema to the prompt, parses what comes back, and validates it. A failure
    to parse or validate returns ``None`` rather than raising, and reflection
    treats that as "this batch produced nothing" rather than an error -- one
    unparseable response should not stop a sweep.
    """

    async def complete(self, prompt: str, model: type[T]) -> T | None:
        """Answer ``prompt`` as an instance of ``model``, or ``None``."""
        ...


class MemoryStore(Protocol):
    """The slice of OpenViking reflection reads and writes.

    Every read here is answerable from the vector index -- the L2 rows carry
    the memory text already, so a sweep never opens a file. Only the two writes
    reach the document store, and only for conclusions.
    """

    async def changed_since(self, moment: datetime, *, limit: int) -> list[str]:
        """Return stored URIs of L2 rows updated after ``moment``, newest first.

        URIs only, no content: this is the change signal, and it runs over
        every memory in the store. It deliberately reads L2 rather than the
        directory overviews, whose refresh lags behind their contents by
        design.
        """
        ...

    async def rows(self, uris: Sequence[str]) -> list[MemoryRow]: ...

    async def neighbours(self, row: MemoryRow, *, limit: int) -> list[MemoryRow]:
        """Return memories semantically near ``row``.

        This is what turns extraction into reflection: the model gets to see
        memories written at other times, which is where an observation that no
        single window could produce comes from.
        """
        ...

    async def tail_sample(self, *, limit: int) -> list[MemoryRow]:
        """Return a few memories at random.

        Ported in spirit from memex's ``_sample_tail_memories``. Without it,
        every memory the model sees was selected for resembling something it
        already believes, and reflection converges on confirming itself.
        """
        ...

    async def read_overview(self, directory: str) -> str | None:
        """Return a directory's generated L1 overview, or ``None``.

        Background for the prompt only. It is model-written, so it is never
        cited -- a quote verified against a summary proves the summary said it,
        not that anything did.
        """
        ...

    async def write_observation(self, observation: Observation) -> str:
        """Persist an observation and return its URI.

        Goes through OpenViking's memory write path rather than a raw file
        write, so the link and merge machinery sees it.
        """
        ...

    async def link(
        self,
        from_uri: str,
        to_uri: str,
        *,
        link_type: str,
        match_text: str | None = None,
        weight: float = 0.5,
    ) -> None:
        """Record a typed edge between two memories.

        ``match_text`` must appear verbatim in the source, which is OpenViking's
        existing contract for the field and the reason reflection's quote check
        is not an extra invention.
        """
        ...
