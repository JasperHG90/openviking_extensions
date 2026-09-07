"""Helpers shared by the ovx test modules."""

from __future__ import annotations

import re

# typer forces a terminal on rich when $GITHUB_ACTIONS, $FORCE_COLOR or
# $PY_COLORS is set (typer/rich_utils.py), so on Actions `ovx --help` comes
# back styled even though nothing is attached to a terminal. Styling splits a
# flag across escape sequences -- "--bind" is written as "-", a colour code,
# then "-bind" -- so a test looking for the literal flag finds nothing and
# reports the whole help as missing.
#
# $CI is NOT one of the three. Reproduce with GITHUB_ACTIONS=1, not CI=1:
# under CI=1 these tests pass whether or not the paint comes off, which makes
# it easy to think you have verified something you have not.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def strip_ansi(text: str) -> str:
    """Return ``text`` without ANSI escape sequences."""
    return _ANSI.sub("", text)
