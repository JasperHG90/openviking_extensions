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

    Matches OpenViking's ``StructuredLLM``: the JSON schema is appended to the
        prompt, the reply is parsed and validated, and a failure to do either
        returns ``None`` rather than raising. Reflection treats that as "this batch
        produced nothing" -- one unparseable response should cost a batch, not a
        sweep.
    """

    async def complete(self, prompt: str, model: type[T]) -> T | None:
        """Answer ``prompt`` as an instance of ``model``, or ``None``."""
        ...


class MemoryStore(Protocol):
    """The slice of OpenViking reflection reads and writes.

    Change detection, evidence and verification are all answerable from the
    vector index -- the L2 rows carry the memory text already. The document
    store is touched for one read per batch, the directory overview used as
    prompt background, and for the two writes, which are conclusions.
    """

    async def changed_since(self, moment: datetime, *, limit: int) -> list[str]:
        """Return stored URIs of L2 rows updated after ``moment``, oldest first.

        URIs only, no content: this is the change signal, and it runs over
        every memory in the store. It deliberately reads L2 rather than the
        directory overviews, whose refresh lags behind their contents by
        design.

        Oldest first, so ``limit`` truncates the newest and the remainder is
        still ahead of the watermark next sweep. Newest-first truncation would
        strand everything below the cut permanently.
        """
        ...

    async def rows(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Return the text and timestamps for specific URIs.

        The text must be the memory itself, not a generated summary: quotes
        are verified against it, and a link whose ``match_text`` came from a
        summary would be absent from the memory it points at.

        Raises
        ------
        ContentUnavailableError
            When the backend stores no row content, so no quote could be
            verified. Refusing beats verifying against the wrong text.
        """
        ...

    async def neighbours(self, row: MemoryRow, *, limit: int) -> list[MemoryRow]:
        """Return memories semantically near ``row``.

        This is what turns extraction into reflection: the model gets to see
        memories written at other times, which is where an observation that no
        single window could produce comes from.
        """
        ...

    async def tail_sample(self, *, limit: int) -> list[MemoryRow]:
        """Return a few memories chosen without regard to the batch.

        Ported in spirit from memex's ``_sample_tail_memories``. Without it,
        every memory the model sees was selected for resembling something it
        already believes, and reflection converges on confirming itself.

        The choice must vary between sweeps. Returning the same rows every time
        is a constant, and a constant cannot break an echo chamber.
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
        """Persist an observation as a memory file and return its URI.

        Written directly rather than through ``remember``, which would hand the
        text to the extractor and get back whatever it decided the text meant.
        The file is still a first-class memory -- indexed, searchable, linkable
        -- because OpenViking's own serializer writes it.
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
