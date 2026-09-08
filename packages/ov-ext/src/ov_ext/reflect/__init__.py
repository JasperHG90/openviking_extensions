"""Reflection over OpenViking's memory.

Extraction sees one window and writes what that window says. Nothing ever goes
back over what it wrote, so a pattern spread across ten memories written on ten
days is never noticed, and two memories that contradict each other sit side by
side unremarked. OpenViking names the seam for this -- ``session/memory/core``
documents a ``ConsolidationExtractContextProvider`` -- and ships only the
abstract base.

This is that pass. One sweep:

1. ask what changed since the watermark;
2. gather the changed memories, their semantic neighbours, and a few at random;
3. one model call for observations, one for contradictions;
4. verify every quote in code -- it must appear in the memory it cites;
5. write what survived.

Observations land as their own memory type, with a ``derived_from`` link per
quote whose ``match_text`` is the quote. That is not a structure invented here:
OpenViking's link vocabulary already has ``derived_from`` for "extracted or
summary facts", and already contracts ``match_text`` to appear verbatim. The
verification step is what makes reflection's links legal rather than plausible.

Ported selectively from memex's ``memory/reflect`` and ``memory/contradiction``.
See ``PROVENANCE.md`` for what came across and what deliberately did not.
"""

from __future__ import annotations

from .config import ENV_PREFIX, ReflectSettings
from .engine import ReflectionEngine, SweepReport
from .models import MemoryRow, Observation
from .ports import MemoryStore, StructuredLLM
from .register import register, unregister
from .watermark import Watermark

__all__ = [
    "ENV_PREFIX",
    "MemoryRow",
    "MemoryStore",
    "Observation",
    "ReflectSettings",
    "ReflectionEngine",
    "StructuredLLM",
    "SweepReport",
    "Watermark",
    "register",
    "unregister",
]
