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
- memex's "skip what is already known" instruction arrives as its own call
  rather than as a line in propose. :func:`revise_prompt` shows the model the
  observation already standing about this entity and the one just drawn, and
  asks what holds now -- so "already known" is a document it reads, not a
  list it is told to imagine.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from .models import ReflectMemoryContext

__all__ = ["consolidate_prompt", "propose_prompt", "revise_prompt"]

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


_CONSOLIDATE_INSTRUCTION = """\
You are given observations drawn from overlapping samples of the same set of \
memories, so several of them are likely to be the same claim in different words.

Group them. Put observations that make the same claim in one group and write a \
single title and content for it. An observation that stands alone gets a group \
of its own.

Drop nothing: every index must appear in exactly one group. If two observations \
are merely related rather than the same, keep them apart -- merging distinct \
claims into one loses both.

Do not invent claims the groups do not support, and do not quote anything: the \
evidence travels with the observations and is attached again afterwards.

STRICT RULE: All output MUST be written in English."""


def consolidate_prompt(observations: Sequence[tuple[str, str]]) -> str:
    """Build the prompt that merges overlapping observations.

    Parameters
    ----------
    observations :
        ``(title, content)`` pairs, in the order their indices refer to.

    Returns
    -------
    str
        The full prompt, without the JSON schema.
    """
    listed = json.dumps(
        [
            {"index": index, "title": title, "content": content}
            for index, (title, content) in enumerate(observations)
        ],
        ensure_ascii=False,
        indent=2,
    )
    return "\n\n---\n\n".join(
        [_CONSOLIDATE_INSTRUCTION, "Observations to group:\n\n" + listed]
    )


_REVISE_INSTRUCTION = """\
An observation about this subject already stands, written by an earlier pass \
over earlier memories. A new one has just been drawn from what has changed \
since. Write the observation that holds now. It replaces the standing one.

Both are about the same subject. That does NOT make them the same claim, and \
merging two claims that merely share a subject destroys them both.

Carry forward what still holds. Drop only what the new claim directly \
supersedes -- a correction replaces what it corrects rather than sitting \
beside it. State a reversal as what is true now, not as a history of what was \
believed.

Merge two statements ONLY when they assert the same thing in different words. \
Anything else is kept: the result carries the standing claim and the new one \
as separate paragraphs, both intact. Dropping a standing claim because the new \
one is unrelated to it is the one mistake you must not make -- there is no \
other copy of it.

Do not invent anything neither side supports, and do not quote: the evidence \
travels with the observations and is attached again afterwards.

STRICT RULE: All output MUST be written in English."""


def revise_prompt(
    standing: tuple[str, str], incoming: tuple[str, str], *, subject: str
) -> str:
    """Build the prompt that folds a new observation into the standing one.

    Parameters
    ----------
    standing :
        ``(title, content)`` of the observation already on disk.
    incoming :
        ``(title, content)`` of the one this sweep drew.
    subject :
        The memory both are filed under, named so the model knows what the file
        is about and does not drift onto whichever claim it read last.

    Returns
    -------
    str
        The full prompt, without the JSON schema.
    """
    payload = json.dumps(
        {
            "subject": subject,
            "standing": {"title": standing[0], "content": standing[1]},
            "new": {"title": incoming[0], "content": incoming[1]},
        },
        ensure_ascii=False,
        indent=2,
    )
    return "\n\n---\n\n".join(
        [_REVISE_INSTRUCTION, "The two observations:\n\n" + payload]
    )
