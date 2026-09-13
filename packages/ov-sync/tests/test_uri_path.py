"""Tests for the rewrite that makes a local path survive as a URI.

The interesting assertion is not that a `#` becomes a `_`. It is that whatever
comes out the other end is something the server takes, so the oracle here is a
transcription of the server's own check rather than a second opinion on it. See
:func:`server_accepts`.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

import pytest

from ov_sync.engine import target_uri
from ov_sync.uri_path import map_uri_paths, safe_relative_path, safe_segment

ROOT = "viking://resources/notes"

# --- The oracle -------------------------------------------------------------
#
# Transcribed verbatim from openviking 0.4.17,
# `openviking/utils/path_safety.py`, which is what the server runs every write
# through. Copied rather than imported: ov-sync talks to OpenViking over HTTP
# and depends on none of its code, and pulling the whole server package in to
# test a hundred lines of path handling is a poor trade. Re-check it against
# that file when the server's rules move.
#
# One thing is left out: the server wraps the URI in a `VikingURI` first, which
# checks the scope (`resources`, `user`, ...) and otherwise hands the string
# back untouched. The scope belongs to the root URI, which the operator sets
# and `ovsync run` checks against the server before it sends anything.

_UNSAFE_REL_PATH_RE = re.compile(r"(^|[\\/])\.\.($|[\\/])")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _reject_encoded_path_escape(path: str) -> None:
    """Raise when a percent-escape hides a separator or a dot segment."""
    for segment in path.replace("\\", "/").split("/"):
        decoded = unquote(segment)
        if decoded != segment and (
            decoded in {".", ".."} or "/" in decoded or "\\" in decoded
        ):
            raise ValueError(f"Unsafe relative path rejected: {path}")


def _sanitize_relative_viking_path(rel_path: str) -> str:
    """Normalize a relative path for use inside Viking URI paths."""
    if not rel_path:
        raise ValueError(f"Unsafe relative path rejected: {rel_path!r}")
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        raise ValueError(f"Unsafe relative path rejected: {rel_path}")
    if _WINDOWS_DRIVE_RE.match(rel_path):
        raise ValueError(f"Unsafe relative path rejected: {rel_path}")
    if _UNSAFE_REL_PATH_RE.search(rel_path):
        raise ValueError(f"Unsafe relative path rejected: {rel_path}")
    _reject_encoded_path_escape(rel_path)
    return rel_path.replace("\\", "/")


def validate_safe_viking_uri_path(uri: str) -> str:
    """Reject ambiguous or traversal-bearing path syntax in a Viking URI."""
    normalized = uri.strip().rstrip("/")
    if "?" in normalized or "#" in normalized:
        raise ValueError(f"Unsafe Viking URI path rejected: {uri}")
    path = normalized[len("viking://") :]
    if not path:
        return normalized
    safe_path = _sanitize_relative_viking_path(path)
    if safe_path != path:
        raise ValueError(f"Unsafe Viking URI path rejected: {uri}")
    return normalized


def server_accepts(uri: str) -> bool:
    """Whether the server would store a write to this URI, or refuse the batch."""
    try:
        return validate_safe_viking_uri_path(uri) == uri
    except ValueError:
        return False


# --- Names that break a URI -------------------------------------------------

# Every one of these is a name a real filesystem allows and a URI does not.
BREAKING_NAMES = [
    "[#TDAI-709] Set up hub and spoke network resources.pdf",
    "what now? notes.md",
    "C#/design.md",
    "a\\b.md",
    "#.md",
    "??",
    "%2F.md",
    "%2f%2f.md",
    "%5C.md",
    "%2e%2E",
    "%2E",
    "trailing space.md ",
    "deep/nested/../not really/#1.md",
    "..",
    # Trimming the tail after the segment rules ran would hand back `..`.
    ".. ",
    ". ",
    "folder/.. ",
]


@pytest.mark.parametrize("name", BREAKING_NAMES)
def test_the_server_refuses_these_names_untouched(name: str) -> None:
    """The fixtures are worth testing against: the server refuses them as-is."""
    assert not server_accepts(f"{ROOT}/{name}")


@pytest.mark.parametrize("name", BREAKING_NAMES)
def test_the_server_takes_them_once_rewritten(name: str) -> None:
    """Whatever the rewrite produces is a URI the server stores."""
    assert server_accepts(target_uri(ROOT, safe_relative_path(name)))


@pytest.mark.parametrize("name", BREAKING_NAMES)
def test_target_uri_rewrites_on_the_way_through(name: str) -> None:
    """The engine's one URI builder is where the rewrite happens."""
    assert server_accepts(target_uri(ROOT, name))


SAFE_NAMES = [
    "notes.md",
    "Invoice%2E2024.pdf",
    "chapter%2Eone/notes.md",
    "projects/acme/spec.md",
    "a file with spaces.md",
    " leading space.md",
    "trailing space in a folder /notes.md",
    "100% done.md",
    "%zz.md",
    "%252F.md",
    "...",
    "a..b.md",
    ".hidden",
    "ünïcode-日本語.md",
    "dash-and_underscore (1).md",
    "v1.2.3+build/report.md",
]


@pytest.mark.parametrize("name", SAFE_NAMES)
def test_a_safe_name_is_left_exactly_as_it_is(name: str) -> None:
    """Nothing that already works may change: a rewrite would re-send the file."""
    assert server_accepts(f"{ROOT}/{name}")
    assert safe_relative_path(name) == name
    assert target_uri(ROOT, name) == f"{ROOT}/{name}"


def test_the_rewrite_is_stable() -> None:
    """Rewriting a rewritten path changes nothing, so the URI never drifts."""
    for name in BREAKING_NAMES:
        once = safe_relative_path(name)
        assert safe_relative_path(once) == once


# Fragments that have each broken one rule or another, to be pasted together
# into names no one would type but a filesystem will happily hold.
# The whitespace entries are deliberately not all plain spaces: the trailing
# trim has to be whatever the server's own str.strip() treats as whitespace,
# and a rewrite that special-cased " " would pass a suite that only tests " ".
_FRAGMENTS = [
    "a",
    "#",
    "?",
    "\\",
    ".",
    "..",
    "%2E",
    "%2e",
    "%2F",
    "%5C",
    "%25",
    " ",
    "\t",
    "\xa0",
    "",
]


@pytest.mark.parametrize("head", _FRAGMENTS)
@pytest.mark.parametrize("tail", _FRAGMENTS)
def test_every_combination_lands_somewhere_the_server_takes(head: str, tail: str) -> None:
    """Brute force over the awkward fragments, two deep and two wide.

    Three properties at once, because they are one property really: whatever
    comes out is accepted, running it again changes nothing, and a name the
    server would have taken is handed back untouched.
    """
    for name in (f"{head}{tail}", f"{head}/{tail}", f"{head}{tail}/notes.md"):
        once = safe_relative_path(name)
        assert server_accepts(target_uri(ROOT, once)), name
        assert safe_relative_path(once) == once, name
        if server_accepts(f"{ROOT}/{name}"):
            assert once == name, name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("[#TDAI-709] plan.pdf", "[_TDAI-709] plan.pdf"),
        ("what? now.md", "what_ now.md"),
        ("a\\b.md", "a_b.md"),
        ("%2Fetc%2Fpasswd", "_2Fetc_2Fpasswd"),
        ("%2e%2e", "_2e_2e"),
        ("%2E", "_2E"),
        ("..", "_"),
        # The server stores a lone `.` as a name, so it is left alone, and an
        # escape that does not decode to a dot segment is not a dot segment.
        (".", "."),
        ("Invoice%2E2024.pdf", "Invoice%2E2024.pdf"),
    ],
)
def test_what_a_rewritten_segment_looks_like(name: str, expected: str) -> None:
    """The rewrite replaces what it must and leaves the rest readable."""
    assert safe_segment(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("[#TDAI-709] plan.pdf", "[_TDAI-709] plan.pdf"),
        ("a/b#c/d?e.md", "a/b_c/d_e.md"),
        ("notes.md   ", "notes.md"),
        ("   ", "_"),
    ],
)
def test_what_a_rewritten_path_looks_like(name: str, expected: str) -> None:
    """A path is its segments, plus the tail the server would have trimmed."""
    assert safe_relative_path(name) == expected


def test_only_the_last_segment_loses_trailing_whitespace() -> None:
    """The server strips the URI's tail, and nothing else, so neither does this.

    A folder named with a trailing space is stored verbatim today; rewriting it
    would move every file under it.
    """
    assert safe_relative_path("folder /notes.md ") == "folder /notes.md"


def test_a_dot_segment_inside_a_path_is_rewritten() -> None:
    """``..`` is traversal to the server wherever it sits, not a folder name."""
    assert safe_relative_path("a/../b.md") == "a/_/b.md"


# --- Collisions -------------------------------------------------------------


def test_paths_that_do_not_collide_are_all_sent() -> None:
    """The ordinary case: every file keeps its place."""
    mapped = map_uri_paths(["a.md", "b/c.md", "d#e.md"])
    assert mapped.safe == {"a.md": "a.md", "b/c.md": "b/c.md", "d#e.md": "d_e.md"}
    assert mapped.rewritten == {"d#e.md": "d_e.md"}
    assert mapped.blocked == {}


def test_the_file_that_owns_the_name_outright_keeps_it() -> None:
    """A rewrite never takes a URI away from the file whose name it already is."""
    mapped = map_uri_paths(["a_b.md", "a#b.md"])
    assert mapped.safe == {"a_b.md": "a_b.md"}
    assert mapped.rewritten == {}
    assert "a#b.md" in mapped.blocked
    assert "'a_b.md'" in mapped.blocked["a#b.md"]


def test_two_rewritten_names_that_clash_are_both_held_back() -> None:
    """With no outright owner there is no principled winner, so nobody goes."""
    mapped = map_uri_paths(["a#b.md", "a?b.md"])
    assert mapped.safe == {}
    assert sorted(mapped.blocked) == ["a#b.md", "a?b.md"]
    assert "'a?b.md'" in mapped.blocked["a#b.md"]
    assert "'a#b.md'" in mapped.blocked["a?b.md"]


def test_three_way_clash_names_every_rival() -> None:
    """The message has to be actionable: it says which files to rename."""
    mapped = map_uri_paths(["a#b.md", "a?b.md", "a\\b.md"])
    reason = mapped.blocked["a#b.md"]
    assert "'a?b.md' and 'a\\\\b.md'" in reason
    assert "also claim." in reason


def test_the_outcome_does_not_depend_on_the_order_files_arrive_in() -> None:
    """Two runs of the same folder must reach the same answer."""
    forwards = map_uri_paths(["a_b.md", "a#b.md", "a?b.md"])
    backwards = map_uri_paths(["a?b.md", "a#b.md", "a_b.md"])
    assert forwards.safe == backwards.safe
    assert sorted(forwards.blocked) == sorted(backwards.blocked)
    assert forwards.blocked["a#b.md"] == backwards.blocked["a#b.md"]


def test_a_collision_across_folders_is_not_a_collision() -> None:
    """Only the whole path has to be unique, not the file name."""
    mapped = map_uri_paths(["one/a#b.md", "two/a#b.md"])
    assert mapped.blocked == {}
    assert mapped.safe == {"one/a#b.md": "one/a_b.md", "two/a#b.md": "two/a_b.md"}


def test_a_file_rewritten_onto_a_folders_name_waits() -> None:
    """One URI cannot be a file and a folder, and disk cannot make this pair."""
    mapped = map_uri_paths(["a#b.md", "a_b.md/note.md"])

    assert mapped.safe == {"a_b.md/note.md": "a_b.md/note.md"}
    assert "folder holding 'a_b.md/note.md'" in mapped.blocked["a#b.md"]


def test_a_folder_rewritten_onto_a_files_name_waits() -> None:
    """Same clash from the other side: the file keeps the name it owns."""
    mapped = map_uri_paths(["a_b.md", "a#b.md/note.md"])

    assert mapped.safe == {"a_b.md": "a_b.md"}
    assert "the file 'a_b.md'" in mapped.blocked["a#b.md/note.md"]


def test_a_folder_and_a_file_that_both_fit_are_left_alone() -> None:
    """The check must not fire on the ordinary case of a folder and a sibling."""
    mapped = map_uri_paths(["a_b.md", "a_b/note.md"])

    assert mapped.blocked == {}
    assert mapped.safe == {"a_b.md": "a_b.md", "a_b/note.md": "a_b/note.md"}


def test_a_rewritten_folder_can_collide_too() -> None:
    """The rewrite runs on every segment, so a folder can take a file's URI."""
    mapped = map_uri_paths(["a#b/notes.md", "a?b/notes.md"])
    assert mapped.safe == {}
    assert sorted(mapped.blocked) == ["a#b/notes.md", "a?b/notes.md"]
