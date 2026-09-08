"""What reflection hands the model, and what it expects back.

Two families live here. The ``Reflect*Context`` models are what a memory looks
like once it has been reduced for a prompt -- an integer id, the text, and when
it happened, nothing else. The rest are output shapes the model fills in.

Ported from memex's ``memory/reflect/prompts.py`` and
``memory/contradiction/signatures.py``, where they are the payloads of DSPy
signatures. The field descriptions are the load-bearing part and are kept
verbatim: they are what the model actually reads, and they encode a lot of
learning about how it goes wrong. What changed is the wrapper -- OpenViking
drives structured output through ``StructuredVLM.complete_model``, which takes
one Pydantic class, so the inputs that were DSPy ``InputField``s are rendered
into the prompt by :mod:`ov_ext.reflect.prompts` instead.

One deliberate divergence: memex cites evidence by an index into a list it
passes the model. So do we, but the index maps to a ``viking://`` URI rather
than a row UUID -- see :mod:`ov_ext.reflect.citations`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

__all__ = [
    "CandidateObservation",
    "ContradictionRelationship",
    "Contradictions",
    "EvidenceItem",
    "MemoryRow",
    "Observation",
    "ProposedObservations",
    "ReflectMemoryContext",
]

# Verbatim from memex. The strictness is deliberate: an earlier, softer wording
# let the model return null indices whenever it felt unsure, which silently
# turned every observation into an unsupported one.
EVIDENCE_INDEX_DESCRIPTION = (
    "The zero-based integer index (or indices) of the source items in the provided "
    "context. Example: 0 for the first item, 1 for the second. "
    "STRICT RULE: Only use Null/None (or an empty list) if the provided context "
    "contains NO specific evidence to support the statement. If evidence is "
    "present in the context, you MUST provide the corresponding integer index."
)


@dataclass(frozen=True)
class MemoryRow:
    """One L2 row, as reflection sees it.

    This is the internal value object, not a boundary type -- it is built from
    a store query rather than parsed from anything, so it is a dataclass rather
    than a model.

    Attributes
    ----------
    uri : str
        The stored ``viking://`` URI, with no level suffix. This is the
        identity reflection cites and links to.
    text : str
        The row's text. For memories this is already
        ``strip_all_links(content)`` truncated by OpenViking, so it is the
        distilled form rather than the raw file.
    created_at : datetime
        When the row was created, shown to the model as ``occurred`` so it can
        prefer observations connecting memories written at different times.
    updated_at : datetime
        When the row last changed. This is what the sweep filters on and what
        the watermark advances to -- advancing on ``created_at`` instead would
        leave a memory created long ago but edited today permanently above the
        mark, re-reflected every sweep.
    """

    uri: str
    text: str
    created_at: datetime
    updated_at: datetime

    @property
    def area(self) -> str:
        """The directory this memory sits in.

        Used as the proxy for "a body of work", so an observation citing two
        areas can be recognised as connecting things that were not written
        together.

        The parent directory rather than a cleverer heuristic, because no
        prefix rule survives the shapes real URIs take. Under
        ``resources/github.com/<owner>/<repo>/`` two repositories are four and
        five segments deep; under ``memories/entities/<category>/`` a category
        is three. Any fixed depth is wrong for one of them -- too shallow and
        two repositories collapse into one area, too deep and every file is its
        own.

        The directory is exact for a store organised by directory, which
        OpenViking's memory types enforce through their ``filename_template``,
        and it never degenerates: two files together are always one area, two
        files apart are always two.
        """
        return self.uri.rsplit("/", 1)[0] if "/" in self.uri else self.uri


class ReflectMemoryContext(BaseModel):
    """A memory reduced for the prompt. Verbatim from memex."""

    index_id: int = Field(
        description="The integer reference ID for this memory (0, 1, ...)."
    )
    content: str = Field(description="The core fact text.")
    occurred: str = Field(description="ISO timestamp or relative time.")


class EvidenceItem(BaseModel):
    """One quote supporting an observation. Verbatim from memex's ``NewEvidenceItem``.

    The ``quote`` is what makes the whole design safe: it must appear in the
    cited memory word for word, which :mod:`ov_ext.reflect.verify` checks in
    code rather than trusting. It also becomes the link's ``match_text``, whose
    OpenViking contract is already "must exist verbatim".
    """

    memory_index: int | None = Field(description=EVIDENCE_INDEX_DESCRIPTION)
    quote: str = Field(
        description="The exact text quote from the memory that supports the observation."
    )
    relevance_explanation: str = Field(
        description=(
            "Explanation of why this quote is relevant/supportive. "
            "DO NOT refer to indices."
        )
    )


class CandidateObservation(BaseModel):
    """An observation the model proposes. Adapted from memex.

    memex splits this across a seed phase that proposes and a validate phase
    that attaches quotes. Reflection here gathers evidence up front and asks
    for both at once, so the candidate carries its own evidence and there is no
    second model call to reconcile.
    """

    title: str = Field(description="Concise title for the observation.")
    content: str = Field(
        description="The proposed observation content describing a pattern or trait."
    )
    evidence: list[EvidenceItem] = Field(
        default_factory=list,
        description="Exact quotes from the provided memories that support this.",
    )


class ProposedObservations(BaseModel):
    """Everything the propose call returns."""

    observations: list[CandidateObservation] = Field(
        default_factory=list,
        description="New observations found. Empty when the memories support none.",
    )


class ContradictionRelationship(BaseModel):
    """One non-neutral relationship between two memories.

    Ported from memex's model of the same name, minus its ``authoritative``
    field. memex uses that to decide which side wins and then applies a
    confidence delta; reflection here only records that the tension exists and
    leaves resolution to a person, so nothing reads a winner.
    """

    left_index: int = Field(description="Index of the first memory in the pair.")
    right_index: int = Field(description="Index of the second memory in the pair.")
    relation: str = Field(
        description=(
            "One of: reinforce, weaken, contradict. Describes how the first "
            "memory relates to the second."
        )
    )
    reasoning: str = Field(
        description="Brief explanation of why this relationship was assigned."
    )


class Contradictions(BaseModel):
    """Everything the contradiction call returns."""

    relationships: list[ContradictionRelationship] = Field(
        default_factory=list,
        description="Non-neutral pairs only. Empty when the memories agree.",
    )


@dataclass(frozen=True)
class Observation:
    """A verified observation, ready to be written.

    The difference from :class:`CandidateObservation` is that every quote here
    has been found in the memory it claims to come from, and the indices have
    been resolved back to URIs.

    Attributes
    ----------
    title : str
        Short name for the observation.
    content : str
        The observation itself.
    evidence : tuple[tuple[str, str], ...]
        ``(uri, quote)`` pairs. Each becomes a ``derived_from`` link whose
        ``match_text`` is the quote.
    areas : frozenset[str]
        Distinct directories the evidence spans. More than one means the
        observation connects memories that were not written together.
    """

    title: str
    content: str
    evidence: tuple[tuple[str, str], ...]
    areas: frozenset[str]

    @property
    def sources(self) -> frozenset[str]:
        """Distinct memories cited, which is what the evidence floor counts."""
        return frozenset(uri for uri, _ in self.evidence)

    @property
    def spans_areas(self) -> bool:
        """Whether the evidence comes from more than one directory."""
        return len(self.areas) > 1
