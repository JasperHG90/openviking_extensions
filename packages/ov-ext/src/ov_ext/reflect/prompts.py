"""The prompt reflection sends, and how the inputs are laid out.

memex declares these as DSPy signatures: a docstring that is the instruction,
typed input fields whose descriptions DSPy renders into the prompt, and typed
output fields it parses back. OpenViking's ``StructuredVLM.complete_model``
takes a prompt string and one Pydantic class instead, so the instruction and
the input descriptions are transcribed here while the output classes live in
:mod:`ov_ext.reflect.models`.

The instruction text is memex's, near-verbatim, because it encodes corrections
for real failure modes -- "skip observations already covered", "most units are
genuinely new". Two departures of our own:

- Propose is told to look across projects, since an observation whose evidence
  spans two of them is the thing single-window extraction can never produce.
- memex's "skip what is already known" instruction is dropped with it: nothing
  here reads existing observations yet, and an instruction referring to a list
  that is never supplied is noise in the prompt. It comes back with the
  compare/merge pass.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from .models import ReflectMemoryContext

__all__ = ["propose_prompt"]

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

Return an empty list rather than padding: memories that support no \
observation are the normal case, not a failure.

STRICT RULE: All observations MUST be written in English, regardless of the \
language of the source memories."""


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

    parts.append(
        "Memories, each with the index you must cite it by:\n\n" + _render(contexts)
    )
    return "\n\n---\n\n".join(parts)
