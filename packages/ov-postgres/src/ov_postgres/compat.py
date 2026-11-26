"""Refuse to start against an OpenViking this package was not tested with.

The ``openviking`` requirement in ``pyproject.toml`` only binds an install that
resolves dependencies. Images that add this package to a prebuilt OpenViking
install it with ``pip install --no-deps``, so pip never reads the cap, and the
adapter would start against whatever OpenViking is there. This module checks
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
ALLOW_UNTESTED_ENV = "OV_POSTGRES_ALLOW_UNTESTED_OPENVIKING"

_RELEASE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
# The first PEP 440 suffix marker decides where a version sorts against its
# release: a pre- or dev-release (0.5.6rc1, 0.5.6.dev1) below it, a post-release
# above it, even with a dev part of its own (0.5.6.post1.dev2) -- as pip agrees.
_SUFFIX_MARKER = re.compile(
    r"alpha|beta|preview|pre|post|rev|rc|dev|a|b|c|r", re.IGNORECASE
)
_BELOW_RELEASE = frozenset(
    {"alpha", "beta", "preview", "pre", "rc", "dev", "a", "b", "c"}
)
_FLAG: TypeAdapter[bool] = TypeAdapter(bool)


def _release(text: str) -> tuple[int, ...] | None:
    """Return a sort key for a version string, or ``None`` if it has none.

    The key is ``major.minor.patch`` followed by 0 for a pre- or dev-release
    and 1 otherwise, so ``0.5.6rc1`` sorts below ``0.5.6`` and a post
    (``0.5.6.post1.dev2`` included) or local release does not.
    """
    match = _RELEASE.match(text)
    if match is None:
        return None
    rest = text[match.end() :].split("+", 1)[0]
    marker = _SUFFIX_MARKER.search(rest)
    final = 0 if marker and marker.group(0).lower() in _BELOW_RELEASE else 1
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
        and ``OV_POSTGRES_ALLOW_UNTESTED_OPENVIKING`` is not set. With it set,
        the same message is logged as a warning instead.
    pydantic.ValidationError
        If ``OV_POSTGRES_ALLOW_UNTESTED_OPENVIKING`` holds something that is
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
        f"ov-postgres is tested against openviking {tested}, but openviking "
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
