"""Extensions to OpenViking, installed into its server process in one call.

OpenViking has no plugin mechanism, so every extension here reaches in the same
way: :func:`ov_ext.install` runs once at startup, before the first search, and
each subsystem patches or registers what it needs. One package rather than
several because the subsystems already depend on each other -- reflection's
synthesized tags feed the diversity pass's tag-similarity leg, and the
retrieval counter that reflection prioritises on has to live in the retriever.

``retrieval``
    A lexical leg fused with OpenViking's vector search, plus an MMR diversity
    pass and a pooled reranker. Ported from memex.
``reflect``
    Periodically re-reads recent memory and synthesizes observations with
    cited evidence. Ported from memex.

Settings are per subsystem and read from the environment: ``OV_RETRIEVAL_`` for
retrieval, ``OV_REFLECT_`` for reflection. The prefixes name the subsystem
rather than the package, so they survive this package being renamed again.
"""

from __future__ import annotations

from .installer import install, uninstall

__all__ = ["__version__", "install", "uninstall"]

try:  # populated by hatch-vcs at build time
    from ._version import __version__
except ImportError:  # editable install or source checkout
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("ov-ext")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"
