"""Refuse to start against an OpenViking this package was not tested with.

The ``openviking`` requirement in ``pyproject.toml`` only binds an install that
resolves dependencies. Images that add this package to a prebuilt OpenViking
install it with ``pip install --no-deps``, so pip never reads the cap, and the
server would patch whatever OpenViking is there. This module checks
at startup instead. ``tests/test_compat.py`` keeps the range here equal to the
one in ``pyproject.toml``.
"""

from __future__ import annotations

import logging
import os
import re
from importlib.metadata import PackageNotFoundError, version

from pydantic import TypeAdapter

__all__ = [
    "ALLOW_UNTESTED_ENV",
    "OPENVIKING_BELOW",
    "OPENVIKING_MIN",
    "check_openviking_version",
]

logger = logging.getLogger(__name__)

# The tested range, as in pyproject.toml: >=0.5.6,<0.6.
OPENVIKING_MIN: tuple[int, ...] = (0, 5, 6)
OPENVIKING_BELOW: tuple[int, ...] = (0, 6)

# Set to a true value to start anyway, with a warning, for a deliberate trial.
ALLOW_UNTESTED_ENV = "OV_EXT_ALLOW_UNTESTED_OPENVIKING"

_RELEASE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
# PEP 440 pre- and dev-release markers. Such a version sorts below its release,
# so 0.5.6rc1 does not satisfy >=0.5.6, as pip agrees.
_PRE_RELEASE = re.compile(r"(?:a|b|c|rc|alpha|beta|pre|preview|dev)\d*", re.IGNORECASE)
_FLAG: TypeAdapter[bool] = TypeAdapter(bool)


def _release(text: str) -> tuple[int, ...] | None:
    """Return a sort key for a version string, or ``None`` if it has none.

    The key is ``major.minor.patch`` followed by 0 for a pre- or dev-release
    and 1 otherwise, so ``0.5.6rc1`` sorts below ``0.5.6`` and a post or local
    release does not.
    """
    match = _RELEASE.match(text)
    if match is None:
        return None
    rest = text[match.end() :].split("+", 1)[0]
    final = 0 if _PRE_RELEASE.search(rest) else 1
    return (*(int(part or 0) for part in match.groups()), final)


def _dotted(parts: tuple[int, ...]) -> str:
    """Render a version tuple as ``0.5.6``."""
    return ".".join(str(part) for part in parts)


def check_openviking_version() -> None:
    """Raise unless the installed OpenViking is in the tested range.

    Raises
    ------
    RuntimeError
        If OpenViking is outside the range, or its version cannot be read,
        and ``OV_EXT_ALLOW_UNTESTED_OPENVIKING`` is not set. With it set,
        the same message is logged as a warning instead.
    pydantic.ValidationError
        If ``OV_EXT_ALLOW_UNTESTED_OPENVIKING`` holds something that is
        not a boolean.
    """
    tested = f">={_dotted(OPENVIKING_MIN)},<{_dotted(OPENVIKING_BELOW)}"
    try:
        installed = version("openviking")
    except PackageNotFoundError:
        installed = "unknown"
    release = _release(installed)
    if (
        release is not None
        and (*OPENVIKING_MIN, 1) <= release
        and release[:3] < OPENVIKING_BELOW
    ):
        return
    message = (
        f"ov-ext is tested against openviking {tested}, but openviking "
        f"{installed} is installed. A --no-deps install skips that requirement; "
        f"install a matching openviking, or set {ALLOW_UNTESTED_ENV}=1 to start "
        "anyway."
    )
    if _FLAG.validate_python(os.environ.get(ALLOW_UNTESTED_ENV, "false")):
        logger.warning(
            "%s Starting anyway because %s is set.", message, ALLOW_UNTESTED_ENV
        )
        return
    raise RuntimeError(message)
