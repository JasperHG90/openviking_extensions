"""Settings for reflection.

Read from the environment with an ``OV_REFLECT_`` prefix, matching how the
retrieval subsystem is configured. The prefix names the subsystem rather than
the package, so renaming the package again does not reach into a deployment.

Defaults are chosen to be dull. Reflection writes to a memory store
unattended, so every knob that could make it write more starts low, and the
whole subsystem starts off.
"""

from __future__ import annotations

import logging
import os
import re
from enum import Enum

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .verify import DEFAULT_MIN_EVIDENCE

__all__ = ["ENV_PREFIX", "RETIRED", "LockKind", "ReflectSettings"]

logger = logging.getLogger(__name__)

ENV_PREFIX = "OV_REFLECT_"

# Settings that existed once and no longer do. Tolerated with a warning instead
# of refused, because refusing happens inside `ov_ext.install()`, which is fatal
# by design -- so a deleted setting would turn a stale line in someone's config
# into a server that will not start, and take retrieval down with reflection.
# The value says what happened, since "unknown setting" is not an explanation.
RETIRED: dict[str, str] = {
    "OV_REFLECT_CONTRADICTIONS": (
        "contradiction detection was removed; a batch is grouped by directory, "
        "so the pairs it found were artifacts of the question rather than "
        "tensions in the store"
    ),
}

# OpenViking's own rule for an identifier segment
# (openviking/core/identifiers.py). Checked here so a value it would reject
# fails while an operator is reading a startup log, rather than inside
# OpenViking's boot where it aborts the server.
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_.@-]+$")


class LockKind(str, Enum):
    """How the sweep is kept to one at a time.

    There is deliberately no default and no "off". Two concurrent sweeps
    corrupt the watermark -- each consumes work the other is half-way through
    -- so the ticker refuses to start until this is set, and setting it is a
    statement about how many processes run.
    """

    UNSET = "unset"
    PROCESS = "process"
    POSTGRES = "postgres"


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

        A name in :data:`RETIRED` is warned about rather than refused. This
        validator runs during ``ov_ext.install()``, which is fatal by design, so
        deleting a setting would otherwise mean a server carrying the old
        variable refuses to boot -- taking retrieval down with it over a line in
        a config file that no longer does anything.

        Raises
        ------
        ValueError
            Naming the unknown variables and the settings that do exist.
        """
        known = {f"{ENV_PREFIX}{name}".upper() for name in type(self).model_fields}
        present = {
            name.upper() for name in os.environ if name.upper().startswith(ENV_PREFIX)
        }
        for name in sorted(present & set(RETIRED)):
            logger.warning(
                "ov-ext reflect: %s is set but no longer does anything (%s). "
                "Remove it from the deployment.",
                name,
                RETIRED[name],
            )
        unknown = sorted(present - known - set(RETIRED))
        if unknown:
            raise ValueError(
                f"Unknown setting(s): {', '.join(unknown)}. "
                f"Valid names are: {', '.join(sorted(known))}"
            )
        return self

    @model_validator(mode="after")
    def _postgres_lock_needs_a_dsn(self) -> ReflectSettings:
        """Refuse a Postgres lock with nowhere to take it.

        Caught here rather than at the first tick, so a deployment that
        misconfigures the lock fails at startup instead of running unlocked
        until someone reads the logs.

        Raises
        ------
        ValueError
            When ``lock`` is postgres and ``lock_dsn`` is empty.
        """
        if self.lock is LockKind.POSTGRES and not self.lock_dsn.strip():
            raise ValueError(f"{ENV_PREFIX}LOCK=postgres requires {ENV_PREFIX}LOCK_DSN.")
        return self

    @model_validator(mode="after")
    def _enabled_reflection_needs_a_usable_user(self) -> ReflectSettings:
        """Refuse to enable reflection without a user OpenViking would accept.

        Emptiness is not the only way to get this wrong: a padded or punctuated
        value passes a "not blank" check and is then rejected by
        ``UserIdentifier`` deep inside OpenViking's startup, which aborts the
        boot. Both are refused here instead, where the message is readable.

        Raises
        ------
        ValueError
            When ``enabled`` is set and ``user_id`` is empty or not a valid
            OpenViking identifier.
        """
        if not self.enabled:
            return self
        if not self.user_id.strip():
            raise ValueError(
                f"{ENV_PREFIX}ENABLED=true requires {ENV_PREFIX}USER_ID: "
                "reflection serves no request, so it has no user to inherit."
            )
        for name, value in (("USER_ID", self.user_id), ("ACCOUNT_ID", self.account_id)):
            if not _IDENTIFIER.match(value):
                raise ValueError(
                    f"{ENV_PREFIX}{name}={value!r} is not a valid OpenViking "
                    "identifier: letters, digits, and _ . @ - only, no spaces."
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
            "Memories drawn into every batch at random from the least recently "
            "updated end of the store. Without them the model only ever sees "
            "what resembles its own candidates, and reflection converges on "
            "confirming itself. 0 disables the safeguard."
        ),
    )
    min_evidence: int = Field(
        default=DEFAULT_MIN_EVIDENCE,
        ge=1,
        description=(
            "Distinct memories an observation must cite to survive. Counted "
            "over sources, not quotes, so three quotes from one paragraph do "
            "not clear a floor of three. Below 2 an observation can rest on a "
            "single memory, which is a restatement rather than a synthesis."
        ),
    )
    require_cross_area: bool = Field(
        default=False,
        description=(
            "Keep only observations whose evidence spans more than one "
            "directory. Off for a general sweep, where same-area observations "
            "are wanted too; on for a pass that exists to find connections "
            "between things written apart."
        ),
    )
    max_stalls: int = Field(
        default=3,
        ge=1,
        description=(
            "Consecutive sweeps that may read something and advance nothing "
            "before the watermark steps over the batch that is blocking it. "
            "A failed batch holds the mark back so an outage cannot drop "
            "memories permanently; without this bound, one batch that fails "
            "every time would block every memory behind it for good. Stepping "
            "over is logged at error level and named in the report."
        ),
    )
    interval_seconds: float = Field(
        default=900.0,
        gt=0,
        description=(
            "Seconds between sweeps, measured from the end of one to the start "
            "of the next -- so a sweep slower than its interval cannot lap "
            "itself. Fifteen minutes by default: reflection reads what changed "
            "since the last run, so a longer gap means bigger batches rather "
            "than lost work."
        ),
    )
    lock: LockKind = Field(
        default=LockKind.UNSET,
        description=(
            "How concurrent sweeps are prevented. `postgres` takes a "
            "pg_try_advisory_lock on OV_REFLECT_LOCK_DSN and is correct for "
            "any number of processes. `process` is an in-process asyncio lock "
            "and is correct only if exactly one process ever sweeps -- "
            "choosing it asserts that. There is no default: two sweeps at once "
            "corrupt the watermark, so the ticker refuses to start until you "
            "say which."
        ),
    )
    user_id: str = Field(
        default="",
        description=(
            "Whose memories the sweep reflects on. Required when the ticker "
            "runs: reflection serves no request, so it has no user to inherit "
            "and must be told one. No default, because guessing would mean "
            "writing observations into somebody's store on the strength of a "
            "guess."
        ),
    )
    account_id: str = Field(
        default="default",
        description="Account the sweep's request context belongs to.",
    )
    lock_dsn: str = Field(
        default="",
        description=(
            "libpq connection string for the advisory lock, required when "
            "`lock` is postgres. Used only to hold the lock, never queried, "
            "though the natural choice is the database already backing the "
            "store."
        ),
    )
    memory_types: list[str] = Field(
        default=["entities"],
        description=(
            "Memory types the sweep reflects on. Entities only, as in memex, "
            "where an observation is by definition 'a synthesized insight about "
            "an entity'. That is what gives an observation somewhere to live and "
            "something to be about. Preferences are excluded on purpose: they are "
            "what the user said about how they want to be worked with, and "
            "synthesizing over them is the agent inventing preferences nobody "
            "expressed."
        ),
    )
    evidence_types: list[str] = Field(
        default=["entities", "events", "resources"],
        description=(
            "What the sweep may draw on as evidence, as opposed to what it "
            "reflects *about* (`memory_types`). Wider on purpose: an observation "
            "is about an entity, but the thing that makes it worth having is "
            "often somewhere else -- an event recording what happened, or a "
            "resource you captured because it was interesting. Preferences are "
            "absent here too: an observation resting on one would read the "
            "user's own instruction back to them as a finding."
        ),
    )
    model: str = Field(
        default="",
        description=(
            "Model the sweep reasons with. Empty inherits the server's, which "
            "is the vision model everything else uses -- and reflection needs "
            "no vision, only the ability to finish. Measured against the lab "
            "deployment: `ollama/glm-5.3-flash` spent ~11k tokens deliberating "
            "and timed out at Bifrost's 120s ceiling on roughly half its calls, "
            "while `ollama/deepseek-v4-flash:0731` answered the same prompts "
            "10 times out of 10 in 20-60s."
        ),
    )
    passes: int = Field(
        default=3,
        ge=1,
        description=(
            "Model calls per sweep, each over a different random subset of the "
            "changed entities. One pass sees one grouping and finds the patterns "
            "that grouping suggests; three see three, and a connection two "
            "entities only make when read together is likelier to come up in at "
            "least one. Costs a call each, so it is the knob for how hard to "
            "look."
        ),
    )
    pass_size: int = Field(
        default=12,
        ge=2,
        description=(
            "Changed entities shown to the model in one pass. Below 2 an "
            "observation cannot clear the evidence floor. The ceiling is what "
            "keeps a prompt answerable -- with deltas rather than whole files, a "
            "dozen is small."
        ),
    )
    deltas_dsn: str = Field(
        default="",
        description=(
            "libpq connection string for the captured-change table. Set it and "
            "the sweep reads what changed in each memory instead of the whole "
            "file, which is the difference between a prompt the model answers "
            "and one it times out on. Needs ov-postgres with `keep_deltas` on."
        ),
    )
    deltas_schema: str = Field(
        default="public",
        description="PostgreSQL schema holding the delta table.",
    )
    context_chars: int = Field(
        default=1200,
        ge=0,
        description=(
            "Most characters of any one memory shown to the model as context -- "
            "a neighbour or a tail sample, never a changed memory, which is "
            "already delta-sized. Quotes are verified against exactly what was "
            "shown, so truncating here cannot produce a citation to text the "
            "model never saw. 0 disables the cap."
        ),
    )
    observations_root: str = Field(
        default="viking://~/memories/observations",
        description=(
            "Where observations are written. Kept apart from memories/entities "
            "on purpose: extracted facts and synthesized claims warrant "
            "different trust, and mixing them makes it impossible to tell which "
            "to doubt when reflection is wrong. Keep it under `memories/`: the "
            "directory overview is generated by a helper that reads the memory "
            "type out of the path segment after `memories`, so a root anywhere "
            "else silently stops producing overviews. Search and the abstract "
            "are unaffected -- those are told the type outright."
        ),
    )
    max_evidence: int = Field(
        default=12,
        ge=2,
        description=(
            "Most quotes one observation file keeps. An observation is revised "
            "in place as the memories behind it change, so without a cap its "
            "evidence list grows for as long as the entity is worked on. The "
            "newest quotes are kept, which also ages out ones whose source has "
            "since been rewritten."
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
