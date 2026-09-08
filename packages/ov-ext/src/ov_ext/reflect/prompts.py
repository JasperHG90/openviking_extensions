"""The two prompts reflection sends, and how the inputs are laid out.

memex declares these as DSPy signatures: a docstring that is the instruction,
typed input fields whose descriptions DSPy renders into the prompt, and typed
output fields it parses back. OpenViking's ``StructuredVLM.complete_model``
takes a prompt string and one Pydantic class instead, so the instruction and
the input descriptions are transcribed here while the output classes live in
:mod:`ov_ext.reflect.models`.

The instruction text is memex's, near-verbatim, because it encodes corrections
for real failure modes -- "skip observations already covered", "only output
NON-NEUTRAL relationships", "most units are genuinely new". Two additions of
our own, both from the design:

- Propose is told to look across projects, since an observation whose evidence
  spans two of them is the thing single-window extraction can never produce.
- Contradiction drops memex's notion of which side wins. Reflection records the
  tension and leaves the resolution to a person, so asking for an authority
  would produce a field nothing reads.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from .models import ReflectMemoryContext

__all__ = ["contradiction_prompt", "propose_prompt"]

_PROPOSE_INSTRUCTION = """\
Analyze a set of memories and generate high-level observations about patterns, \
preferences, decisions, and recurring themes.

An observation is a claim that no single memory states on its own. Restating \
one memory is not an observation. Prefer observations that connect memories \
which were written at different times, or that come from different projects -- \
those are the ones nobody could have seen while writing any one of them.

Every observation MUST cite the memories that support it, by their integer \
index, with an EXACT quote copied from the memory text. Do not paraphrase a \
quote: it is checked against the source word for word, and an observation \
whose quotes cannot be found is discarded.

Skip anything already covered by the observations listed as known. Return an \
empty list rather than padding: memories that support no observation are the \
normal case, not a failure.

STRICT RULE: All observations MUST be written in English, regardless of the \
language of the source memories."""

_CONTRADICTION_INSTRUCTION = """\
Identify pairs of memories that are in tension with each other.

For each pair, decide whether the first memory reinforces, weakens, or \
contradicts the second:

- reinforce: both hold, and the first strengthens the second
- weaken: both could hold, but the first makes the second less likely
- contradict: they cannot both be true

Most memories are simply about different things -- do not invent tension. \
ONLY output non-neutral pairs; skip neutral ones entirely. An empty list is \
the expected answer for a set of memories that agree.

Do not decide which memory is correct. Recording that the tension exists is \
the whole task; a person resolves it.

STRICT RULE: All reasoning MUST be written in English."""


def _render(contexts: Sequence[ReflectMemoryContext]) -> str:
    """Lay the memories out as indexed JSON objects.

    JSON rather than prose because the index has to survive intact -- a
    numbered markdown list invites the model to renumber when it reformats,
    and a renumbered citation resolves to the wrong memory rather than
    failing loudly.
    """
    return json.dumps(
        [context.model_dump() for context in contexts],
        ensure_ascii=False,
        indent=2,
    )


def propose_prompt(
    contexts: Sequence[ReflectMemoryContext],
    *,
    scope: str | None = None,
    known: Sequence[str] = (),
) -> str:
    """Build the prompt asking for new observations.

    Parameters
    ----------
    contexts :
        Memories to reason over, already numbered.
    scope :
        The directory overview covering the memories that changed, used as
        background. Never cited: it is generated text, so a quote found in it
        proves only that the summary said so. Omitted when there is none.
    known :
        Titles of observations that already exist, so the model does not
        propose them again.

    Returns
    -------
    str
        The full prompt, without the JSON schema -- ``complete_model`` appends
        that itself.
    """
    parts = [_PROPOSE_INSTRUCTION]

    if scope:
        parts.append(
            "Background on the area these memories come from. Context only -- "
            "it is generated text, so never quote or cite it:\n\n" + scope.strip()
        )

    if known:
        listed = "\n".join(f"- {title}" for title in known)
        parts.append("Observations already known. Do not repeat these:\n\n" + listed)

    parts.append("Memories, each with the index you must cite it by:\n\n" + _render(contexts))
    return "\n\n---\n\n".join(parts)


def contradiction_prompt(contexts: Sequence[ReflectMemoryContext]) -> str:
    """Build the prompt asking which memories are in tension.

    Parameters
    ----------
    contexts :
        Memories to compare, already numbered. The same numbering as the
        propose call, so a pair can be resolved with one citation map.

    Returns
    -------
    str
        The full prompt, without the JSON schema.
    """
    return "\n\n---\n\n".join(
        [
            _CONTRADICTION_INSTRUCTION,
            "Memories, each with the index you must refer to it by:\n\n"
            + _render(contexts),
        ]
    )
