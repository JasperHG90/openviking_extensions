"""Put :class:`HybridRetriever` in front of OpenViking's own retriever.

OpenViking builds its retriever inline, twice, in
``openviking/storage/viking_fs/_semantic.py``. There is no factory, registry or
setting to point at a different class, so the only way in without forking is to
replace the name both sites resolve.

Both do so with a *function-local* import from
``openviking.retrieve.hierarchical_retriever``, which is what makes this safe
enough to rely on: the name is looked up at call time from one canonical
module, so rebinding that single attribute reaches both sites, and any site
added later, without patching either of them.

This is still a monkeypatch on somebody else's package. :func:`install`
verifies what it is replacing and refuses to guess, so an upstream change
surfaces as a clear error at startup rather than as retrieval quietly
reverting to vector-only months later.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import HybridSettings
from .rerank import install_pooled_rerank, uninstall_pooled_rerank
from .retriever import HybridRetriever

__all__ = ["install", "uninstall"]

logger = logging.getLogger(__name__)

_MODULE = "openviking.retrieve.hierarchical_retriever"
_ATTRIBUTE = "HierarchicalRetriever"

# The class displaced by install(), so uninstall() can put it back. Module
# state because the patch is process-global: there is nothing else to hang it
# on, and a second install() is a no-op rather than a second layer.
_original: type[Any] | None = None


def install(settings: HybridSettings | None = None) -> None:
    """Replace OpenViking's retriever with the hybrid one, process-wide.

    Call once at startup, before the first search. Calling again is harmless
    and does not stack another wrapper.

    Parameters
    ----------
    settings :
        Behaviour toggles baked into every retriever built afterwards. Read
        from the environment when omitted.

    Raises
    ------
    RuntimeError
        If the expected class is not where it should be, or is not a class.
        Better a refused startup than retrieval silently losing its keyword
        leg and its diversity pass.
    pydantic.ValidationError
        If ``settings`` is omitted and the environment holds a bad or
        misspelled ``OV_RETRIEVAL_`` variable, since the settings are then read
        here. Also a deliberate startup failure: a misspelled variable is one
        the operator believes is in effect.
    """
    global _original

    # Read before anything is patched, so a bad environment fails cleanly
    # rather than half way through.
    resolved = settings or HybridSettings()

    import importlib

    module = importlib.import_module(_MODULE)
    current = getattr(module, _ATTRIBUTE, None)
    if current is None:
        raise RuntimeError(
            f"{_MODULE}.{_ATTRIBUTE} is missing. This OpenViking version does "
            "not lay out its retriever where ov-retrieval expects; upgrade or "
            "pin ov-retrieval rather than running unpatched."
        )
    if not isinstance(current, type):
        raise RuntimeError(
            f"{_MODULE}.{_ATTRIBUTE} is {type(current)!r}, not a class. Refusing "
            "to replace something whose shape is not understood."
        )
    # After both refusals above, so a startup this function is going to reject
    # leaves nothing patched behind it. Otherwise the RuntimeError path would
    # exit with the rerank client swapped and nobody left to call uninstall().
    #
    # Independent of the retriever swap itself: pooling helps whoever calls the
    # rerank client, patched retriever or not, so it happens even on the
    # already-installed path below.
    if resolved.rerank_pooling:
        install_pooled_rerank()

    # `issubclass`, not `is`: what gets installed is a subclass carrying the
    # settings, so an identity check would miss its own previous patch, record
    # it as the original, and leave uninstall() restoring a patch instead of
    # OpenViking's class.
    if issubclass(current, HybridRetriever):
        logger.debug("ov-retrieval: already installed")
        return

    _original = current

    # Rebuilt per install so the settings are captured now rather than read
    # from the environment on every construction.
    class _ConfiguredHybridRetriever(HybridRetriever):
        """A HybridRetriever carrying the settings install() was given."""

        def __init__(self, **kwargs: Any) -> None:
            kwargs.setdefault("settings", resolved)
            super().__init__(**kwargs)

    _ConfiguredHybridRetriever.__name__ = HybridRetriever.__name__
    _ConfiguredHybridRetriever.__qualname__ = HybridRetriever.__qualname__

    setattr(module, _ATTRIBUTE, _ConfiguredHybridRetriever)
    logger.info("ov-retrieval: hybrid retrieval installed over %s", current.__name__)


def uninstall() -> None:
    """Restore OpenViking's own retriever.

    Exists mostly so a test can undo the patch: it is process-global state,
    and a test that installs without restoring changes every test after it.
    Does nothing when nothing was installed.
    """
    global _original

    uninstall_pooled_rerank()

    if _original is None:
        return

    import importlib

    module = importlib.import_module(_MODULE)
    setattr(module, _ATTRIBUTE, _original)
    _original = None
    logger.info("ov-retrieval: restored OpenViking's retriever")
