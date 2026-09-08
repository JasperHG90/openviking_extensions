"""Settings for reflection.

Read from the environment with an ``OV_REFLECT_`` prefix, matching how the
retrieval subsystem is configured. The prefix names the subsystem rather than
the package, so renaming the package again does not reach into a deployment.

Defaults are chosen to be dull. Reflection writes to a memory store
unattended, so every knob that could make it write more starts low, and the
whole subsystem starts off.
"""

from __future__ import annotations

import os

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .verify import DEFAULT_MIN_EVIDENCE

__all__ = ["ENV_PREFIX", "ReflectSettings"]

ENV_PREFIX = "OV_REFLECT_"


class ReflectSettings(BaseSettings):
    """How the reflection sweep behaves."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="forbid")

    @model_validator(mode="after")
    def _reject_misspelled_variables(self) -> ReflectSettings:
        """Refuse an ``OV_REFLECT_`` variable that matches no setting.

        ``extra="forbid"`` does not cover this: pydantic-settings looks up the
        fields it knows and never enumerates the environment, so
        ``OV_REFLECT_MIN_EVIDNCE`` is not rejected -- it is never read, and the
        default silently stands. Someone who set it would watch reflection
        ignore them with nothing to explain why. The retrieval subsystem
        refuses these the same way.

        Raises
        ------
        ValueError
            Naming the unknown variables and the settings that do exist.
        """
        known = {f"{ENV_PREFIX}{name}".upper() for name in type(self).model_fields}
        unknown = sorted(
            name
            for name in os.environ
            if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
        )
        if unknown:
            raise ValueError(
                f"Unknown setting(s): {', '.join(unknown)}. "
                f"Valid names are: {', '.join(sorted(known))}"
            )
        return self

    enabled: bool = Field(
        default=False,
        description=(
            "Run the sweep. Off by default: reflection writes to memory without "
            "anyone watching, so switching it on should be a decision someone "
            "made rather than one they inherited."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Do everything except write. The sweep still reads, prompts and "
            "verifies, and reports what it would have written -- which is how "
            "you find out what a prompt change does before it reaches the store."
        ),
    )
    batch_limit: int = Field(
        default=50,
        ge=1,
        description=(
            "Most changed memories to look at in one sweep. A ceiling rather "
            "than a target: it bounds both the model spend and how much a bad "
            "prompt can write in one go."
        ),
    )
    neighbour_limit: int = Field(
        default=8,
        ge=0,
        description=(
            "Semantic neighbours gathered per changed memory. This is what "
            "lets an observation reach past the window that produced it; 0 "
            "reduces reflection to summarising what just changed."
        ),
    )
    tail_sample: int = Field(
        default=3,
        ge=0,
        description=(
            "Memories drawn at random into every batch. Without them the model "
            "only ever sees what resembles its own candidates, and reflection "
            "converges on confirming itself. 0 disables the safeguard."
        ),
    )
    min_evidence: int = Field(
        default=DEFAULT_MIN_EVIDENCE,
        ge=1,
        description=(
            "Verified quotes an observation needs to survive. Below 2 an "
            "observation can rest on a single memory, which is a restatement "
            "rather than a synthesis."
        ),
    )
    require_cross_peer: bool = Field(
        default=False,
        description=(
            "Keep only observations whose evidence spans more than one project. "
            "Off for a general sweep, where single-project observations are "
            "wanted too; on for a pass that exists to find connections between "
            "separate bodies of work."
        ),
    )
    contradictions: bool = Field(
        default=True,
        description=(
            "Ask which memories are in tension and record a `contradicts` link "
            "for each pair. Costs one model call per batch over memories that "
            "have already been gathered."
        ),
    )
    observations_root: str = Field(
        default="viking://~/memories/observations",
        description=(
            "Where observations are written. Kept apart from memories/entities "
            "on purpose: extracted facts and synthesized claims warrant "
            "different trust, and mixing them makes it impossible to tell which "
            "to doubt when reflection is wrong."
        ),
    )
    state_path: str = Field(
        default="viking://~/resources/reflect/watermark.json",
        description=(
            "Where the sweep records how far it has read. Kept in the store "
            "rather than on local disk so a sweep run from anywhere resumes "
            "from the same place."
        ),
    )
