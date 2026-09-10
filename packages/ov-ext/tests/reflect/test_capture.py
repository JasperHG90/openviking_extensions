"""Capturing the delta OpenViking applies, at the moment it applies it.

Built on OpenViking's own classes rather than stand-ins -- ``StrPatch``,
``SearchReplaceBlock``, ``DeleteBlock``, ``ResolvedOperation`` and
``MemoryUpdateResult`` are the real ones. A fake would agree with whatever this
module assumed, which is the one thing these tests exist to disprove.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from openviking.session.memory.dataclass import (
    MemoryFile,
    ResolvedOperation,
    ResolvedOperations,
)
from openviking.session.memory.memory_updater import MemoryUpdateResult, MemoryUpdater
from openviking.session.memory.merge_op.base import (
    DeleteBlock,
    SearchReplaceBlock,
    StrPatch,
)

from ov_ext.reflect.capture import MemoryDelta, deltas_from, install, uninstall

URI = "viking://user/jasper/memories/entities/dev_tool/ov_ext.md"
OTHER = "viking://user/jasper/memories/entities/dev_tool/ov_dash.md"


class Collector:
    """A sink that keeps what it is given."""

    def __init__(self) -> None:
        self.batches: list[list[MemoryDelta]] = []

    async def record(self, deltas: Sequence[MemoryDelta]) -> None:
        self.batches.append(list(deltas))

    @property
    def deltas(self) -> list[MemoryDelta]:
        return [delta for batch in self.batches for delta in batch]


class BrokenSink:
    """A sink that always fails, standing in for a database that is down."""

    async def record(self, deltas: Sequence[MemoryDelta]) -> None:
        raise RuntimeError("postgres unreachable")


def edit(
    uri: str = URI, *, blocks: list[Any], field: str = "content"
) -> ResolvedOperation:
    """An operation that patches an existing memory.

    The previous body is built to contain every block's search text, because
    that is what a patch the extractor really emitted looks like -- and because
    capture drops blocks whose search is absent, a placeholder body would make
    every test here pass for the wrong reason.
    """
    previous = "\n".join(
        text for text in (getattr(block, "search", "") for block in blocks) if text
    )
    return ResolvedOperation(
        old_memory_file_content=MemoryFile(uri=uri, content=previous or "empty"),
        memory_fields={field: StrPatch(blocks=blocks)},
        memory_type="entities",
        uris=[uri],
    )


def create(uri: str = URI, *, content: str) -> ResolvedOperation:
    """An operation that writes a memory that did not exist."""
    return ResolvedOperation(
        old_memory_file_content=None,
        memory_fields={"content": content},
        memory_type="entities",
        uris=[uri],
    )


def applied(
    *, written: list[str] | None = None, edited: list[str] | None = None
) -> MemoryUpdateResult:
    """A result reporting these URIs as having landed."""
    result = MemoryUpdateResult()
    for uri in written or []:
        result.add_written(uri)
    for uri in edited or []:
        result.add_edited(uri)
    return result


def operations(*ops: ResolvedOperation) -> ResolvedOperations:
    return ResolvedOperations(
        upsert_operations=list(ops), delete_file_contents=[], errors=[]
    )


# --- what a delta is -------------------------------------------------------


async def test_a_search_replace_block_becomes_the_change_it_describes() -> None:
    ops = operations(
        edit(blocks=[SearchReplaceBlock(search="- ships v0.3", replace="- ships v0.4")])
    )

    deltas = await deltas_from(ops, applied(edited=[URI]))

    assert len(deltas) == 1
    assert deltas[0].uri == URI
    assert deltas[0].memory_type == "entities"
    assert deltas[0].field == "content"
    assert deltas[0].search == "- ships v0.3"
    assert deltas[0].replace == "- ships v0.4"
    assert deltas[0].created is False


async def test_a_delete_block_is_captured_through_the_same_interface() -> None:
    """DeleteBlock exposes search/replace as properties; one accessor must cover both."""
    ops = operations(edit(blocks=[DeleteBlock(delete="- an obsolete claim")]))

    deltas = await deltas_from(ops, applied(edited=[URI]))

    assert len(deltas) == 1
    assert deltas[0].search == "- an obsolete claim"
    assert deltas[0].replace == ""
    assert deltas[0].is_deletion


async def test_a_created_memory_is_recorded_as_its_own_delta() -> None:
    """A create has no patch, so the whole (small, new) memory is the change."""
    ops = operations(create(content="# ov-clip\n\nA browser extension."))

    deltas = await deltas_from(ops, applied(written=[URI]))

    assert len(deltas) == 1
    assert deltas[0].created is True
    assert deltas[0].search == ""
    assert deltas[0].replace.startswith("# ov-clip")


async def test_every_block_in_one_patch_is_captured_separately() -> None:
    ops = operations(
        edit(
            blocks=[
                SearchReplaceBlock(search="alpha", replace="ALPHA"),
                SearchReplaceBlock(search="beta", replace="BETA"),
                DeleteBlock(delete="gamma"),
            ]
        )
    )

    deltas = await deltas_from(ops, applied(edited=[URI]))

    assert [(d.search, d.replace) for d in deltas] == [
        ("alpha", "ALPHA"),
        ("beta", "BETA"),
        ("gamma", ""),
    ]


async def test_the_raw_dict_shape_is_accepted_too() -> None:
    """The updater sees an uncoerced dict when a reply was parsed but not validated."""
    operation = ResolvedOperation(
        old_memory_file_content=MemoryFile(uri=URI, content="old"),
        memory_fields={"content": {"blocks": [{"search": "old", "replace": "new"}]}},
        memory_type="entities",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(edited=[URI]))

    assert [(d.search, d.replace) for d in deltas] == [("old", "new")]


# --- only what actually landed ---------------------------------------------


async def test_an_operation_that_failed_leaves_no_delta() -> None:
    """apply_operations collects errors instead of raising, so the batch handed
    in is a list of intentions. Recording those would invent history."""
    ops = operations(edit(blocks=[SearchReplaceBlock(search="a", replace="b")]))
    result = MemoryUpdateResult()
    result.add_error(URI, ValueError("patch did not match"))

    assert await deltas_from(ops, result) == []


async def test_only_the_operation_that_landed_is_recorded() -> None:
    ops = operations(
        edit(URI, blocks=[SearchReplaceBlock(search="a", replace="b")]),
        edit(OTHER, blocks=[SearchReplaceBlock(search="c", replace="d")]),
    )
    result = applied(edited=[URI])
    result.add_error(OTHER, ValueError("boom"))

    deltas = await deltas_from(ops, result)

    assert [d.uri for d in deltas] == [URI]


async def test_a_no_op_block_is_not_a_change() -> None:
    """OpenViking's own validation skips search == replace; a delta here would
    make the memory look edited on every sweep."""
    ops = operations(edit(blocks=[SearchReplaceBlock(search="same", replace="same")]))

    assert await deltas_from(ops, applied(edited=[URI])) == []


async def test_a_non_text_field_on_a_create_is_not_invented_into_a_change() -> None:
    operation = ResolvedOperation(
        old_memory_file_content=None,
        memory_fields={"content": "real text", "category": "dev_tool", "count": 3},
        memory_type="entities",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(written=[URI]))

    # `count` is not text at all. `category` is, but it is a label rather than
    # body prose -- both are recorded as fields, and it is the reader's job to
    # pick the body. What must not happen is `count` becoming a change.
    assert "count" not in {d.field for d in deltas}
    assert "content" in {d.field for d in deltas}


# --- only what really applied ----------------------------------------------


async def test_a_block_that_never_matched_is_not_recorded() -> None:
    """apply_str_patch raises only when EVERY block fails.

    A two-block patch where one matches returns normally and the URI lands in
    `edited_uris`, so a per-URI filter alone would record the block that never
    applied as history that happened.
    """
    operation = ResolvedOperation(
        old_memory_file_content=MemoryFile(uri=URI, content="alpha line here"),
        memory_fields={
            "content": StrPatch(
                blocks=[
                    SearchReplaceBlock(search="alpha line here", replace="ALPHA"),
                    SearchReplaceBlock(search="zeta never present", replace="ZETA"),
                ]
            )
        },
        memory_type="entities",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(edited=[URI]))

    assert [(d.search, d.replace) for d in deltas] == [("alpha line here", "ALPHA")]


async def test_a_created_memory_records_no_search_side() -> None:
    """A create arrives as a patch too, but its blocks match nothing.

    OpenViking builds a new memory's body by concatenating the blocks' `replace`
    sides, so the `search` sides name text that was never in any memory.
    """
    operation = ResolvedOperation(
        old_memory_file_content=None,
        memory_fields={
            "content": StrPatch(
                blocks=[
                    SearchReplaceBlock(
                        search="# ov-clip", replace="# ov-clip\n\nAn extension."
                    ),
                    SearchReplaceBlock(
                        search="## Notes", replace="## Notes\n\n- ships v0.1"
                    ),
                ]
            )
        },
        memory_type="entities",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(written=[URI]))

    assert len(deltas) == 2
    assert all(d.created for d in deltas)
    assert all(d.search == "" for d in deltas), "a create has nothing it replaced"
    assert not any(d.is_deletion for d in deltas), "a new memory deletes nothing"


# --- the wrapper on the real class -----------------------------------------


@pytest.fixture(autouse=True)
def _uninstall_after_each_test() -> Iterator[None]:
    yield
    uninstall()


async def test_installing_wraps_the_real_updater_and_captures() -> None:
    """The whole mechanism, against the class every memory upsert really uses."""
    sink = Collector()
    ops = operations(edit(blocks=[SearchReplaceBlock(search="- v0.3", replace="- v0.4")]))
    expected = applied(edited=[URI])

    async def fake_apply(
        self: Any, operations: Any, *args: Any, **kwargs: Any
    ) -> MemoryUpdateResult:
        return expected

    original = MemoryUpdater.apply_operations
    MemoryUpdater.apply_operations = fake_apply
    try:
        install(sink)
        returned = await MemoryUpdater().apply_operations(ops, None)
    finally:
        uninstall()
        MemoryUpdater.apply_operations = original

    assert returned is expected, "the caller must get the updater's own result back"
    assert [(d.search, d.replace) for d in sink.deltas] == [("- v0.3", "- v0.4")]


async def test_uninstall_puts_the_original_method_back() -> None:
    original = MemoryUpdater.apply_operations
    install(Collector())
    assert MemoryUpdater.apply_operations is not original
    uninstall()
    assert MemoryUpdater.apply_operations is original


async def test_installing_twice_does_not_stack_two_wrappers() -> None:
    sink = Collector()
    original = MemoryUpdater.apply_operations
    install(sink)
    once = MemoryUpdater.apply_operations
    install(sink)
    assert MemoryUpdater.apply_operations is once
    uninstall()
    assert MemoryUpdater.apply_operations is original


async def test_a_failing_sink_never_breaks_the_memory_write() -> None:
    """The memory is already written by the time the sink is called."""
    ops = operations(edit(blocks=[SearchReplaceBlock(search="a", replace="b")]))
    expected = applied(edited=[URI])

    async def fake_apply(
        self: Any, operations: Any, *args: Any, **kwargs: Any
    ) -> MemoryUpdateResult:
        return expected

    original = MemoryUpdater.apply_operations
    MemoryUpdater.apply_operations = fake_apply
    try:
        install(BrokenSink())
        returned = await MemoryUpdater().apply_operations(ops, None)
    finally:
        uninstall()
        MemoryUpdater.apply_operations = original

    assert returned is expected


def test_install_refuses_when_the_method_has_moved(monkeypatch: Any) -> None:
    """Better a refused startup than capture that silently never runs."""
    monkeypatch.delattr(MemoryUpdater, "apply_operations")
    with pytest.raises(RuntimeError, match="apply_operations"):
        install(Collector())


# --- what the updater really matched against -------------------------------


async def test_a_patch_to_a_non_content_field_is_captured() -> None:
    """`soul`, `identity`, `skills` and `tools` patch fields that are not `content`.

    `_apply_upsert` reads those from `extra_fields`, not from the rendered body.
    Comparing them against the body drops every edit the trainers make.
    """
    operation = ResolvedOperation(
        old_memory_file_content=MemoryFile(
            uri=URI,
            content="unrelated prose",
            extra_fields={"core_truths": "- ships on Fridays"},
        ),
        memory_fields={
            "core_truths": StrPatch(
                blocks=[
                    SearchReplaceBlock(
                        search="- ships on Fridays", replace="- ships on Tuesdays"
                    )
                ]
            )
        },
        memory_type="soul",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(edited=[URI]))

    assert [(d.field, d.search, d.replace) for d in deltas] == [
        ("core_truths", "- ships on Fridays", "- ships on Tuesdays")
    ]


async def test_a_block_matching_what_an_earlier_block_produced_is_kept() -> None:
    """Block N is matched against blocks 1..N-1's output, not the original."""
    operation = ResolvedOperation(
        old_memory_file_content=MemoryFile(uri=URI, content="alpha gamma"),
        memory_fields={
            "content": StrPatch(
                blocks=[
                    SearchReplaceBlock(search="alpha", replace="beta"),
                    SearchReplaceBlock(search="beta gamma", replace="delta"),
                ]
            )
        },
        memory_type="entities",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(edited=[URI]))

    assert [(d.search, d.replace) for d in deltas] == [
        ("alpha", "beta"),
        ("beta gamma", "delta"),
    ]


async def test_an_overlapping_block_that_could_not_land_is_dropped() -> None:
    """Both searches are in the original, but the second cannot apply after the first.

    A substring test against the original admits both. Only replaying catches it.
    """
    operation = ResolvedOperation(
        old_memory_file_content=MemoryFile(uri=URI, content="one two three"),
        memory_fields={
            "content": StrPatch(
                blocks=[
                    SearchReplaceBlock(search="one two", replace="ONE TWO"),
                    SearchReplaceBlock(search="two three", replace="TWO THREE"),
                ]
            )
        },
        memory_type="entities",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(edited=[URI]))

    assert [(d.search, d.replace) for d in deltas] == [("one two", "ONE TWO")]


async def test_a_field_gaining_its_first_value_is_captured() -> None:
    """`None` and `""` are different answers upstream, not two spellings of one.

    `PatchOp.apply` routes a `None` to `_extract_replace_when_no_original` and
    writes the blocks' joined `replace` sides; `""` goes to the matcher and
    finds nothing. A `skills` memory that later gains `guidelines` takes the
    first branch, so collapsing them loses every first write to a patch field.
    """
    operation = ResolvedOperation(
        old_memory_file_content=MemoryFile(
            uri=URI, content="body", extra_fields={"recommendation": "use it"}
        ),
        memory_fields={
            "guidelines": StrPatch(
                blocks=[
                    SearchReplaceBlock(
                        search="- deploy on green", replace="- deploy on green"
                    )
                ]
            )
        },
        memory_type="skills",
        uris=[URI],
    )

    deltas = await deltas_from(operations(operation), applied(edited=[URI]))

    assert [(d.field, d.search, d.replace) for d in deltas] == [
        ("guidelines", "", "- deploy on green")
    ]


async def test_a_uri_that_both_landed_and_failed_is_dropped() -> None:
    """The result reports URIs, not operations.

    Two operations can touch one memory in a batch. When one succeeds and one
    fails, there is no way out here to tell which blocks moved it, so the URI is
    dropped -- losing real deltas rather than inventing false ones.
    """
    ops = operations(
        edit(URI, blocks=[SearchReplaceBlock(search="alpha", replace="beta")]),
        edit(URI, blocks=[SearchReplaceBlock(search="delta", replace="DELTA")]),
    )
    result = applied(edited=[URI])
    result.add_error(URI, ValueError("second operation did not apply"))

    assert await deltas_from(ops, result) == []


async def test_upstream_still_raises_or_changes_on_a_single_block() -> None:
    """Pins the invariant the `after == working` guard is held in reserve for.

    Capture treats "the text changed" as the definition of applied, while
    OpenViking defines it as `applied_count`. Today a one-block apply either
    raises or changes the text, so the two agree. If that stops being true this
    fails, rather than capture quietly recording a block that did nothing.
    """
    from openviking.session.memory.merge_op.base import FieldType
    from openviking.session.memory.merge_op.patch import PatchOp

    patch_op = PatchOp(FieldType.STRING)
    before = "hello world"

    with pytest.raises(Exception):
        await patch_op.apply(
            before, StrPatch(blocks=[SearchReplaceBlock(search="absent", replace="X")])
        )

    after = await patch_op.apply(
        before, StrPatch(blocks=[SearchReplaceBlock(search="hello", replace="goodbye")])
    )
    assert after != before
