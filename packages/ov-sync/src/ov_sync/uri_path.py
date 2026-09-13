"""Turn a local path into one OpenViking will accept as a URI.

A file's path under the folder is its path under the root, but disk allows
names a ``viking://`` URI does not. The server refuses a few characters
outright — a batch carrying one fails whole, taking every other file in it
down — and silently trims a few more, which is worse: the file lands somewhere
the state file does not point.

Both are fixed here, before the URI is built. The rewrite touches only what the
server would refuse or change, so a name that is already safe comes back
untouched and nothing that synced before syncs again. That last property is the
one to protect when changing anything here: a rewrite that fires on a name the
server would have taken re-uploads the file under a new URI and orphans the old
one.

Two files can want the same name once one of them is rewritten. That is a
clash, and :func:`map_uri_paths` refuses to resolve it by guessing: the file
whose own name is already safe keeps the URI, and the rest are held back for
the operator to rename. Letting them through would mean one file silently
overwriting another.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import unquote

# What an unsafe character becomes. The server's own segment sanitizer uses an
# underscore, so a rewritten name looks like something OpenViking produced.
REPLACEMENT = "_"

# `#` opens a fragment and `?` a query, so the server rejects either anywhere in
# a URI. A `\` is read as a Windows separator and normalized to `/`, which the
# server sees as a path it had to rewrite, and it rejects those too.
_UNSAFE_CHARS_RE = re.compile(r"[#?\\]")

# The server decodes every segment to catch traversal smuggled in as an escape.
# `%2F` and `%5C` uncover a separator wherever they sit, so the server refuses
# them outright; breaking the `%` leaves the name readable and the escape inert.
_SEPARATOR_ESCAPE_RE = re.compile(r"%(2[Ff]|5[Cc])")

# `%2E` is `.`, but the server objects only when the whole segment decodes to a
# dot segment. `Invoice%2E2024.pdf` decodes to `Invoice.2024.pdf` and is stored
# as it is, so rewriting it would re-upload a file that already syncs.
_DOT_SEGMENTS = frozenset({".", ".."})


@dataclass(frozen=True)
class UriPathMap:
    """Where each of a folder's files lands, and which cannot land at all.

    Attributes
    ----------
    safe : dict[str, str]
        Relative path to the path it takes inside the root URI, for every file
        that can be sent.
    rewritten : dict[str, str]
        The entries of ``safe`` whose URI path differs from the file's own
        name, so a caller can say so out loud.
    blocked : dict[str, str]
        Relative path to the reason it cannot be sent. Only clashes land here:
        a rewrite on its own never blocks a file.
    """

    safe: dict[str, str]
    rewritten: dict[str, str]
    blocked: dict[str, str]


def safe_segment(name: str) -> str:
    """Return one path component with everything the server refuses replaced.

    Parameters
    ----------
    name :
        A single file or folder name, with no separators in it.

    Returns
    -------
    str
        The name itself when it is already safe, otherwise a copy with each
        unsafe character replaced by an underscore.
    """
    # `..` is traversal to the server wherever it sits. A lone `.` is not: the
    # server stores it as a name, so rewriting it would be a rewrite nobody
    # asked for. Neither can come out of a directory walk; they are handled so
    # this function's promise holds whatever it is passed.
    if name == "..":
        return REPLACEMENT
    defused = _SEPARATOR_ESCAPE_RE.sub(rf"{REPLACEMENT}\1", name)
    decoded = unquote(defused)
    if decoded != defused and decoded in _DOT_SEGMENTS:
        defused = defused.replace("%", REPLACEMENT)
    return _UNSAFE_CHARS_RE.sub(REPLACEMENT, defused)


def safe_relative_path(relative_path: str) -> str:
    """Return the path a file takes inside the root URI.

    Parameters
    ----------
    relative_path :
        Path relative to the synced folder, forward-slashed.

    Returns
    -------
    str
        The path unchanged when every component is already safe, otherwise the
        rewritten one.
    """
    segments = relative_path.split("/")
    # The server strips the whole URI before parsing it, so whitespace at the
    # very end -- the tail of the last segment -- is dropped after ov-sync has
    # written down where the file went. Trailing whitespace anywhere else is
    # kept verbatim by the server, so it is kept here too.
    #
    # Trimmed before the segment rules run, not after: `.. ` is a name the
    # server takes, and trimming it afterwards would hand back `..`, the one
    # thing these rules exist to keep out.
    segments[-1] = segments[-1].rstrip()
    safe = [safe_segment(segment) for segment in segments]
    safe[-1] = safe[-1] or REPLACEMENT
    return "/".join(safe)


def map_uri_paths(relative_paths: Iterable[str]) -> UriPathMap:
    """Work out where a folder's files land, and which ones clash.

    Parameters
    ----------
    relative_paths :
        Every file in scope, relative to the synced folder. Order decides
        nothing; a clash is settled by the names themselves.

    Returns
    -------
    UriPathMap
        The files that can be sent, the ones whose name changed on the way, and
        the ones held back because another file has a better claim to the name.
    """
    claims: dict[str, list[str]] = {}
    for relative_path in relative_paths:
        claims.setdefault(safe_relative_path(relative_path), []).append(relative_path)

    safe: dict[str, str] = {}
    blocked: dict[str, str] = {}
    for uri_path, owners in claims.items():
        # The file that already carries the name outright keeps it. When no
        # such file exists every claimant was rewritten into the same name, and
        # there is no principled winner, so none of them goes.
        keeper = owners[0] if len(owners) == 1 else _outright_owner(owners, uri_path)
        for owner in owners:
            if owner == keeper:
                safe[owner] = uri_path
                continue
            rivals = sorted(rival for rival in owners if rival != owner)
            blocked[owner] = (
                f"its name becomes {uri_path!r} in OpenViking, which "
                f"{_and_list(rivals)} also claim{'s' if len(rivals) == 1 else ''}. "
                "Rename one of them, or exclude it."
            )

    _block_folder_clashes(safe, blocked)
    safe = {
        relative_path: uri_path
        for relative_path, uri_path in safe.items()
        if relative_path not in blocked
    }
    rewritten = {
        relative_path: uri_path
        for relative_path, uri_path in safe.items()
        if relative_path != uri_path
    }
    return UriPathMap(safe=safe, rewritten=rewritten, blocked=blocked)


def _block_folder_clashes(safe: dict[str, str], blocked: dict[str, str]) -> None:
    """Hold back files whose rewritten name is also a folder's name.

    A rewrite can turn a file into the twin of a folder: `a#b.md` becomes
    `a_b.md`, and a folder called `a_b.md` is already sending `a_b.md/note.md`.

    OpenViking takes the pair without complaint — both write, both stat — and
    that is the trap. The file wins the node: `ls` on it returns nothing, so
    every file under it is invisible to listing while still reachable by a
    direct `stat`. It does not heal on its own, and writing more files into the
    folder does not help. Against a live server: removing the file leaves the
    listing empty too, and only a write *after* that removal brings it back,
    buried children and all.

    Disk cannot produce this pair on its own, so the rewrite does not produce
    it either: the rewritten side gives way.

    ``blocked`` gains an entry per file held back; the caller drops those from
    ``safe``.
    """
    folders = {
        parent for uri_path in safe.values() for parent in folder_prefixes(uri_path)
    }
    for relative_path, uri_path in sorted(safe.items()):
        if uri_path not in folders:
            continue
        inside = sorted(
            other
            for other, other_path in safe.items()
            if other_path.startswith(f"{uri_path}/")
        )
        if relative_path != uri_path:
            blocked[relative_path] = (
                f"its name becomes {uri_path!r} in OpenViking, which is the "
                f"folder holding {_and_list(inside)}. Rename it, or exclude it."
            )
            continue
        # The file carries the name outright, so it is the folder that was
        # rewritten onto it, and the files inside it are the ones that wait.
        for other in inside:
            if other != safe[other]:
                blocked[other] = (
                    f"its folder becomes {uri_path!r} in OpenViking, which is "
                    f"the file {relative_path!r}. Rename one of them, or "
                    "exclude it."
                )


def folder_prefixes(uri_path: str) -> list[str]:
    """Return every folder a path sits under, outermost first."""
    segments = uri_path.split("/")
    return ["/".join(segments[:depth]) for depth in range(1, len(segments))]


def _outright_owner(owners: list[str], uri_path: str) -> str | None:
    """Return the file already named ``uri_path``, or None when there is none.

    At most one can match: the scanner never reports the same path twice.
    """
    return next((owner for owner in owners if owner == uri_path), None)


def _and_list(names: list[str]) -> str:
    """Join names for a sentence: ``'a'``, ``'a' and 'b'``, ``'a', 'b' and 'c'``."""
    quoted = [repr(name) for name in names]
    if len(quoted) == 1:
        return quoted[0]
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"
