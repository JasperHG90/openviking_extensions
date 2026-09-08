"""The one call that installs every ov-ext subsystem into OpenViking.

OpenViking has no plugin mechanism, so each subsystem reaches into the running
server on its own terms -- retrieval by rebinding a class, and whatever follows
by its own route. This module is the single entry point that runs them, so a
deployment has one thing to call and one place to look when something did not
take effect.

Failures are fatal by design. A server that came up silently missing its
keyword leg looks healthy and answers worse -- far harder to notice than a
refused startup.
"""

from __future__ import annotations

import logging

from .retrieval.config import HybridSettings
from .retrieval.patch import install as install_retrieval
from .retrieval.patch import uninstall as uninstall_retrieval

__all__ = ["install", "uninstall"]

logger = logging.getLogger(__name__)


def install(retrieval: HybridSettings | None = None) -> None:
    """Install every subsystem, process-wide.

    Call once at startup, before the first search. Calling again is harmless:
    each subsystem's own install is idempotent and does not stack a second
    layer.

    Parameters
    ----------
    retrieval :
        Retrieval behaviour. Read from the environment when omitted.

    Raises
    ------
    RuntimeError
        If a subsystem cannot attach -- for example when OpenViking has moved
        the class retrieval rebinds. Better a refused startup than an extension
        that silently did nothing.
    """
    retrieval_settings = retrieval or HybridSettings()
    install_retrieval(retrieval_settings)

    logger.info(
        "ov-ext: keyword=%s mmr=%s lambda=%.2f pool=x%d",
        retrieval_settings.keyword_enabled,
        retrieval_settings.mmr_enabled,
        retrieval_settings.mmr_lambda,
        retrieval_settings.pool_factor,
    )


def uninstall() -> None:
    """Undo :func:`install`, restoring what OpenViking had before.

    Subsystems are removed in reverse order of installation. One that was never
    installed is skipped rather than treated as an error.
    """
    uninstall_retrieval()
