"""The one call that installs every ov-ext subsystem into OpenViking.

Named ``installer`` rather than ``install`` because ``__init__`` re-exports the
function: with both called ``install``, ``ov_ext.install`` resolves to the
function and the module becomes unreachable by name -- which broke two tests
before anyone noticed the ambiguity.

OpenViking has no plugin mechanism, so each subsystem reaches into the running
server on its own terms -- retrieval by rebinding a class it builds inline,
reflection by registering a memory type through the documented custom-templates
setting. This module is the single entry point that runs them, so a deployment
has one thing to call and one place to look when something did not take effect.

All of that patches the process it runs in, which is the process serving
requests only while ``workers = 1``. Past one, uvicorn spawns children that
inherit nothing, so :mod:`ov_ext.worker` arranges for each of them to install
as well.

Order matters in one direction: retrieval is patched before reflection is
registered, so a sweep that searches for evidence gathers it through the
patched retriever rather than the stock one.

A failure to patch is fatal by design. A server that came up silently missing
its keyword leg looks healthy and answers worse -- far harder to notice than a
refused startup.
"""

from __future__ import annotations

import logging

from .reflect.config import ReflectSettings
from .reflect.patch import install as install_reflect_ticker
from .reflect.patch import uninstall as uninstall_reflect_ticker
from .reflect.registration import register as register_reflect
from .reflect.registration import unregister as unregister_reflect
from .retrieval.config import HybridSettings
from .retrieval.patch import install as install_retrieval
from .retrieval.patch import uninstall as uninstall_retrieval
from .worker import install as install_worker_bridge
from .worker import uninstall as uninstall_worker_bridge

__all__ = ["install", "uninstall"]

logger = logging.getLogger(__name__)


def install(
    retrieval: HybridSettings | None = None,
    reflect: ReflectSettings | None = None,
) -> None:
    """Install every subsystem, process-wide.

    Call once at startup, before the first search. Calling again is harmless:
    each subsystem's own install is idempotent and does not stack a second
    layer.

    Reflection, when enabled, also arranges for its sweep ticker to start
    when OpenViking's service finishes booting -- that is where the filesystem,
    the vector store and an event loop first exist together. The ticker holds a
    lock for every sweep, and refuses to start if no lock has been chosen.

    Parameters
    ----------
    retrieval :
        Retrieval behaviour. Read from the environment when omitted.
    reflect :
        Reflection behaviour. Read from the environment when omitted. Disabled
        by default.

    Raises
    ------
    RuntimeError
        If a subsystem cannot attach -- for example when OpenViking has moved
        the class retrieval rebinds. Better a refused startup than an extension
        that silently did nothing.
    """
    retrieval_settings = retrieval or HybridSettings()
    reflect_settings = reflect or ReflectSettings()

    install_retrieval(retrieval_settings)
    register_reflect(reflect_settings)
    install_reflect_ticker(reflect_settings)
    # Everything above patched *this* process. With more than one uvicorn
    # worker the process serving requests is a spawned child that has run none
    # of it, so this arranges for the children to install too.
    install_worker_bridge()

    logger.info(
        "ov-ext: keyword=%s mmr=%s lambda=%.2f pool=x%d reflect=%s",
        retrieval_settings.keyword_enabled,
        retrieval_settings.mmr_enabled,
        retrieval_settings.mmr_lambda,
        retrieval_settings.pool_factor,
        reflect_settings.enabled,
    )


def uninstall() -> None:
    """Undo :func:`install`, restoring what OpenViking had before.

    Subsystems are removed in reverse order of installation, so reflection is
    unregistered before the retriever it searches through is put back. One that
    was never installed is skipped rather than treated as an error.
    """
    uninstall_worker_bridge()
    uninstall_reflect_ticker()
    unregister_reflect()
    uninstall_retrieval()
