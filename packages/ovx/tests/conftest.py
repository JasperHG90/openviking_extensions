"""Helpers shared by the ovx test modules."""

from __future__ import annotations

import re

# Rich decides on its own whether to colour, and on CI it decides yes: the
# runner sets CI, so `ovx --help` comes back styled even though nothing is
# attached to a terminal. Styling splits a flag across escape sequences --
# "--bind" is written as "-", a colour code, then "-bind" -- so a test looking
# for the literal flag finds nothing and reports the whole help as missing.
# Every assertion here is about what the help says, not how it is painted, so
# the paint comes off first.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def strip_ansi(text: str) -> str:
    """Return ``text`` without ANSI escape sequences."""
    return _ANSI.sub("", text)
