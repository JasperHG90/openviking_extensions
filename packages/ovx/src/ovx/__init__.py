"""ovx — run ov against a named profile, without leaving an API key on disk."""

from __future__ import annotations

__all__ = ["__version__"]

try:
    # Written at build time by hatch-vcs, which reads the version from the
    # ovx-v* git tag. It is not tracked, so a source tree that has never been
    # built has none.
    from ovx._version import __version__
except ImportError:  # pragma: no cover - only in an unbuilt source tree
    __version__ = "0.0.0.dev0"
