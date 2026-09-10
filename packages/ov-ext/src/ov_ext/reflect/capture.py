"""Record what changed in a memory, at the moment OpenViking changes it.

Reflection needs the delta, not the document. An OpenViking memory is one file
that accretes for months -- ``entities/software_project/openviking.md`` is 20 KB
-- while the thing worth reflecting on is the handful of lines that changed. Ask
for the file and you get a prompt no model will answer in time; ask for the
delta and you get roughly what memex reflects on, which is one extracted fact.

There is nowhere to read that delta from after the fact. OpenViking keeps no
version history unless someone calls the snapshot API, the index holds one row
per file, and a line diff between two copies of an extractor-rewritten document
reports a reflow as a change. But the delta exists *before* the write: a memory
field whose ``merge_op`` is ``patch`` is updated by applying a ``StrPatch`` --
SEARCH/REPLACE and DELETE blocks -- and those blocks are exactly the statement
"this is what changed". They are applied and then dropped.

So this catches them on the way through. ``MemoryUpdater.apply_operations`` is
the one method every memory upsert passes: the streaming updater, the
compressor, the trainers and the extractor all construct a ``MemoryUpdater`` and
call it. Wrapping it once on the class catches every path.

Only what actually landed is recorded, as far as the result allows.
``apply_operations`` does not raise on a failed operation -- it collects the
error and carries on -- so the batch handed in is a list of *intentions*. Three
filters narrow it to what happened: the URI must be in the result's written or
edited list, it must not also be in its errors, and each block is replayed
through OpenViking's own matcher and kept only if it changes the text.

One gap is not closeable from out here. ``MemoryUpdateResult`` reports URIs, not
operations, so when two operations touch the same memory in one batch there is
no way to tell which of them moved it. Such a URI is dropped, which loses real
deltas rather than inventing false ones. The same seam means ``before`` is the
operation's pre-fetched copy while the updater re-reads from disk, so a second
patch to a memory already patched in the same batch replays against stale text.
The reflect sweep can live with both: a missed delta is re-derived the next time
that memory changes.

Not everything is covered, and the gaps are structural rather than bugs:

- A new memory has no previous text. It still usually arrives as a patch --
  OpenViking concatenates the blocks' ``replace`` sides to build the body -- but
  the ``search`` sides name nothing that was ever there, so they are dropped and
  the delta is recorded with ``created`` set and an empty ``search``.
- A field whose ``merge_op`` is ``immutable`` never patches. Every field of the
  ``cases`` memory type is immutable, so a case is created once and never
  yields a delta again.
- A direct ``content/write`` bypasses this entirely: that path reindexes but
  never calls ``apply_operations``. Reflection's own observation writes go that
  way, which is what keeps reflection from reflecting on itself.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

__all__ = ["DeltaSink", "MemoryDelta", "deltas_from", "install", "uninstall"]

logger = logging.getLogger(__name__)

_MODULE = "openviking.session.memory.memory_updater"
_CLASS = "MemoryUpdater"
_METHOD = "apply_operations"

# The method displaced by install(), so uninstall() can put it back. Module
# state because the patch is process-global, as is the sink it feeds.
_original: Any = None


@dataclass(frozen=True)
class MemoryDelta:
    """One change to one memory, as the writer described it.

    Attributes
    ----------
    uri : str
        The memory that changed.
    memory_type : str
        Its OpenViking memory type, for example ``entities``.
    field : str
        The memory field the change lands in, usually ``content``.
    search : str
        The text that was there. Empty when the memory is new, and empty on the
        replace half of a create.
    replace : str
        The text that replaced it. Empty for a deletion.
    created : bool
        Whether the memory did not exist before this write. A created memory has
        no ``search`` side, so a reader must not treat it as an edit of nothing.
    changed_at : datetime
        When the change was recorded, in UTC.
    """

    uri: str
    memory_type: str
    field: str
    search: str
    replace: str
    created: bool
    changed_at: datetime

    @property
    def is_deletion(self) -> bool:
        """Whether this change removed text without putting anything back."""
        return bool(self.search) and not self.replace


class DeltaSink(Protocol):
    """Somewhere to put captured deltas.

    Structural rather than inherited, so the store that implements it -- a
    Postgres table, a list in a test -- does not import this module.
    """

    async def record(self, deltas: Sequence[MemoryDelta]) -> None:
        """Persist a batch of deltas. Called once per applied operation batch."""
        ...


def _blocks_of(patch_value: Any) -> list[Any]:
    """Return the patch blocks in ``patch_value``, or an empty list.

    Accepts the ``StrPatch`` the extractor produces and the raw dict shape that
    reaches the updater when a reply was parsed but not yet coerced -- the same
    two forms OpenViking's own patch validation accepts.
    """
    blocks = getattr(patch_value, "blocks", None)
    if blocks is not None:
        return list(blocks)
    if isinstance(patch_value, dict) and isinstance(patch_value.get("blocks"), list):
        return list(patch_value["blocks"])
    return []


def _block_sides(block: Any) -> tuple[str, str] | None:
    """Return ``(search, replace)`` for one patch block, or ``None`` to skip it.

    ``DeleteBlock`` exposes the same two names as ``SearchReplaceBlock`` through
    properties -- ``search`` is the doomed text and ``replace`` is empty -- so
    one accessor covers both and a new block type with the same shape needs no
    change here.
    """
    if isinstance(block, dict):
        if "delete" in block:
            return str(block["delete"]), ""
        if "search" in block:
            return str(block["search"]), str(block.get("replace", ""))
        return None
    search = getattr(block, "search", None)
    if search is None:
        return None
    return str(search), str(getattr(block, "replace", "") or "")


def _previous_value(operation: Any, field: str) -> str | None:
    """The field's value before this operation, or ``None`` when it had none.

    Resolved exactly the way ``MemoryUpdater._apply_upsert`` resolves it:
    ``content`` from the rendered body, every other field from
    ``extra_fields``. Using the body for all of them would compare a
    ``soul.core_truths`` patch against prose that never contains it, and drop
    every edit to the four memory types whose patched fields are not
    ``content``.

    ``None`` and ``""`` are different answers, not two spellings of one.
    ``PatchOp.apply`` routes a ``None`` to ``_extract_replace_when_no_original``
    and writes the blocks' joined ``replace`` sides, while ``""`` goes to the
    matcher and finds nothing. Collapsing them loses every first write to a
    patch field -- a ``skills`` memory that later gains ``guidelines``, a
    ``tools`` one that gains ``optimal_params``.
    """
    before = getattr(operation, "old_memory_file_content", None)
    if before is None:
        return None
    try:
        if field == "content":
            return str(before.plain_content() or "")
        value = (before.extra_fields or {}).get(field)
        return None if value is None else str(value)
    except Exception as exc:
        # A MemoryFile that cannot render its own value is not worth failing a
        # capture over. Logged rather than swallowed: silence here looks
        # exactly like a memory that did not change.
        logger.debug(
            "ov-ext reflect: could not read %s before the patch", field, exc_info=exc
        )
        return None


async def _applied_blocks(
    blocks: Sequence[Any], before: str | None
) -> list[tuple[str, str]]:
    """Return the ``(search, replace)`` pairs that really changed the text.

    Replays the blocks through OpenViking's own ``PatchOp`` rather than
    reasoning about what it would have done. Three things make the guessing
    version wrong, and all three fall out of replaying:

    * ``apply_str_patch`` raises only when *no* block matches, so a block that
      never landed rides along on a batch that succeeded.
    * Block *N* is matched against what blocks 1..N-1 produced, not against the
      original, so a search built by an earlier block is legitimate.
    * Its matcher is fuzzy above a threshold, so a block can land with text that
      is not literally in the source, and a substring test would reject it.

    A block is kept only when applying it changes the working text -- which is
    exactly OpenViking's own definition in ``_validate_patch_operations``.

    Parameters
    ----------
    blocks :
        The patch blocks, in the order the model emitted them.
    before :
        The field's value before the patch, or ``None`` when it had none --
        passed through untouched, because upstream treats the two differently.

    Returns
    -------
    list[tuple[str, str]]
        One pair per block that changed the text, in order. When ``before`` is
        ``None`` the field had no value and the blocks are its first, so each
        non-empty ``replace`` is returned with an empty ``search``.
    """
    from openviking.session.memory.merge_op.base import FieldType, StrPatch
    from openviking.session.memory.merge_op.patch import PatchOp

    if before is None:
        # The field is being populated for the first time. Upstream does not
        # match anything here -- `_extract_replace_when_no_original` joins the
        # blocks' `replace` sides and writes that -- so there is nothing to
        # replay against, and the blocks carry no history. A first write also
        # tends to arrive with `search` equal to `replace`, since the model has
        # nothing to search for, which the no-op guard below would drop.
        return [
            ("", replace)
            for _, replace in (
                sides
                for sides in (_block_sides(block) for block in blocks)
                if sides is not None
            )
            if replace.strip()
        ]

    patch_op = PatchOp(FieldType.STRING)
    working: str = before
    applied: list[tuple[str, str]] = []

    for block in blocks:
        sides = _block_sides(block)
        if sides is None:
            continue
        search, replace = sides
        if search == replace:
            # OpenViking's own validation skips these, and a no-op recorded as
            # a change would be a memory that looks edited every sweep.
            continue
        try:
            after = await patch_op.apply(working, StrPatch(blocks=[block]))
        except Exception as exc:
            # A block that raises did not land. The batch as a whole may still
            # have succeeded on its other blocks. Logged, because a shape bug
            # here would otherwise present as "this memory did not change".
            logger.debug(
                "ov-ext reflect: block did not replay, not recorded", exc_info=exc
            )
            continue
        if after == working:
            # Belt and braces. Measured against OpenViking 0.4.17, a one-block
            # apply either raises or changes the text, so this cannot fire
            # today -- but "applied" is defined upstream as `applied_count`,
            # not as "the text differs", and a future block type that succeeds
            # without changing anything would otherwise be recorded as history.
            continue
        working = after
        applied.append((search, replace))

    return applied


async def deltas_from(operations: Any, result: Any) -> list[MemoryDelta]:
    """Extract the deltas that a completed ``apply_operations`` actually wrote.

    Two filters, because a URI landing is not proof that every block did.
    ``apply_str_patch`` raises only when *no* block matched, so a batch that
    succeeded can still carry blocks that never applied. The URI filter catches
    a whole operation that failed; :func:`_applied_blocks` catches the rest by
    replaying each block through OpenViking's own matcher and keeping only the
    ones that changed the text.

    Parameters
    ----------
    operations :
        The ``ResolvedOperations`` handed to the updater -- what it was asked to
        do.
    result :
        The ``MemoryUpdateResult`` it returned -- what it managed to do. Only
        URIs it reports as written or edited are kept, so an operation that
        failed leaves no delta behind claiming otherwise.

    Returns
    -------
    list[MemoryDelta]
        One entry per applied patch block per field per applied URI. Empty when
        nothing applied, which includes a batch that failed outright.
    """
    landed = set(getattr(result, "written_uris", []) or [])
    landed |= set(getattr(result, "edited_uris", []) or [])
    # A URI can be in both: `apply_operations` records success and failure per
    # operation, and two operations in one batch can touch the same memory. The
    # result says which URIs moved, not which operations moved them, so a URI
    # that also failed is dropped rather than credited to whichever operation
    # is asked about it. That loses real deltas in a mixed batch; recording
    # invented ones would be worse.
    failed = {uri for uri, _ in getattr(result, "errors", []) or []}
    landed -= failed
    if not landed:
        return []

    now = datetime.now(timezone.utc)
    deltas: list[MemoryDelta] = []

    for operation in getattr(operations, "upsert_operations", []) or []:
        uris = [uri for uri in (operation.uris or []) if uri in landed]
        if not uris:
            continue
        created = not operation.is_edit()
        memory_type = str(getattr(operation, "memory_type", "") or "")

        for field, value in (getattr(operation, "memory_fields", {}) or {}).items():
            blocks = _blocks_of(value)
            if not blocks:
                # A create can also arrive as a plain string rather than a
                # patch. Anything else -- a number, a list, an immutable field
                # echoed back unchanged -- is not a text change and is left out
                # rather than stringified into one.
                if created and isinstance(value, str) and value.strip():
                    for uri in uris:
                        deltas.append(
                            MemoryDelta(
                                uri=uri,
                                memory_type=memory_type,
                                field=str(field),
                                search="",
                                replace=value,
                                created=True,
                                changed_at=now,
                            )
                        )
                continue

            if created:
                # A new memory arrives as a patch too, and OpenViking builds its
                # body by concatenating the blocks' `replace` sides
                # (`_extract_replace_when_no_original`) rather than matching
                # anything. There is no previous text to replay against, and the
                # blocks' `search` sides name nothing that was ever there.
                pairs = [
                    ("", replace)
                    for _, replace in (
                        sides
                        for sides in (_block_sides(block) for block in blocks)
                        if sides is not None
                    )
                    if replace.strip()
                ]
            else:
                pairs = await _applied_blocks(
                    blocks, _previous_value(operation, str(field))
                )

            for search, replace in pairs:
                for uri in uris:
                    deltas.append(
                        MemoryDelta(
                            uri=uri,
                            memory_type=memory_type,
                            field=str(field),
                            search=search,
                            replace=replace,
                            created=created,
                            changed_at=now,
                        )
                    )

    return deltas


def install(sink: DeltaSink) -> None:
    """Record every applied memory change into ``sink``.

    Wraps ``MemoryUpdater.apply_operations`` on the class, so every updater --
    the streaming one, the compressor, the trainers -- is covered by one rebind.
    Installing twice does not stack two wrappers.

    Parameters
    ----------
    sink :
        Where captured deltas go. Its failures are logged and swallowed: this
        sits on the path that writes the user's memories, and losing a delta is
        better than losing the memory it describes.

    Raises
    ------
    RuntimeError
        If the method to wrap is not where it should be, so an upstream rename
        surfaces as a refused startup rather than as capture silently never
        running.
    """
    global _original
    if _original is not None:
        # Said out loud rather than swallowed: a second caller passing a
        # different sink would otherwise watch its deltas go to the first one.
        logger.warning(
            "ov-ext reflect: delta capture is already installed; the sink passed "
            "to this call is ignored. Call uninstall() first to replace it."
        )
        return

    import importlib

    module = importlib.import_module(_MODULE)
    updater_class = getattr(module, _CLASS, None)
    if updater_class is None:
        raise RuntimeError(f"{_MODULE} has no {_CLASS}; OpenViking has moved it")
    original = getattr(updater_class, _METHOD, None)
    if original is None or not callable(original):
        raise RuntimeError(f"{_CLASS} has no callable {_METHOD}; OpenViking has moved it")

    async def apply_and_capture(
        updater: Any, operations: Any, *args: Any, **kwargs: Any
    ) -> Any:
        """Apply the operations, then record what landed."""
        result = await original(updater, operations, *args, **kwargs)
        try:
            deltas = await deltas_from(operations, result)
            if deltas:
                await sink.record(deltas)
        except Exception:
            # Broad on purpose. The memory has already been written; a sink
            # that is down must not turn a successful write into an exception
            # in OpenViking's own extraction path.
            logger.exception("ov-ext reflect: could not record memory deltas")
        return result

    _original = original
    setattr(updater_class, _METHOD, apply_and_capture)
    logger.info("ov-ext reflect: capturing memory deltas")


def uninstall() -> None:
    """Put OpenViking's method back. Safe to call when nothing was installed."""
    global _original
    if _original is None:
        return

    import importlib

    updater_class = getattr(importlib.import_module(_MODULE), _CLASS)
    setattr(updater_class, _METHOD, _original)
    _original = None
