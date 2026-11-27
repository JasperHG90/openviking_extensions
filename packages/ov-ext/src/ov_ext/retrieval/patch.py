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
verifies what it is replacing, and the private backend methods the keyword leg
calls, and refuses to guess, so an upstream change surfaces as a clear error at
startup rather than as retrieval quietly reverting to vector-only months later.

One retriever is deliberately left alone. The Skills API's package search,
``SkillPackageRetriever``, subclasses ``HierarchicalRetriever`` at import time,
so rebinding the name never reaches it. Patching it would buy nothing: it has
its own ``retrieve_skills``, which pages the vector search and collapses files
into packages, and calls neither ``retrieve`` nor ``_rerank_scores`` -- the two
methods this package overrides. ``find`` and ``search`` with a skill context
type still go through the patched retriever.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from .config import HybridSettings
from .rerank import install_pooled_rerank, uninstall_pooled_rerank
from .retriever import HybridRetriever

__all__ = ["install", "uninstall"]

logger = logging.getLogger(__name__)

_MODULE = "openviking.retrieve.hierarchical_retriever"
_ATTRIBUTE = "HierarchicalRetriever"

# Arguments the keyword leg passes to the backend's `_build_scope_filter`. A
# required parameter outside this set is one ov-ext does not know to supply.
_SCOPE_ARGUMENTS = frozenset(
    {
        "self",
        "ctx",
        "context_type",
        "target_directories",
        "extra_filter",
        "level",
        "acl_enabled",
    }
)

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
        If the expected class is not where it should be, is not a class, or
        the backend methods the keyword leg calls have changed shape. Better a
        refused startup than retrieval silently losing its keyword leg and its
        diversity pass.
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
            "not lay out its retriever where ov-ext expects; upgrade or "
            "pin ov-ext rather than running unpatched."
        )
    if not isinstance(current, type):
        raise RuntimeError(
            f"{_MODULE}.{_ATTRIBUTE} is {type(current)!r}, not a class. Refusing "
            "to replace something whose shape is not understood."
        )
    _check_backend_contract()
    if not callable(getattr(current, "_rerank_scores", None)):
        raise RuntimeError(
            f"{_MODULE}.{_ATTRIBUTE} has no _rerank_scores. The document cap "
            "overrides it; this OpenViking reranks some other way."
        )

    # After the refusals above, so a startup this function is going to reject
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
        logger.debug("ov-ext: already installed")
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
    logger.info("ov-ext: hybrid retrieval installed over %s", current.__name__)


def _check_backend_contract() -> None:
    """Refuse to start if the keyword leg's backend calls no longer fit.

    The keyword leg scopes its search with the vector store's private
    ``_build_scope_filter`` and asks ``_acl_enabled`` for the flag it needs.
    It catches failures at query time and falls back to vector-only, which is
    right for an outage and wrong for a signature change: the server would
    look healthy and lose its keyword leg on every search. So the signature is
    checked here, once.

    Raises
    ------
    RuntimeError
        If either method is missing, or the scope builder requires an argument
        ov-ext does not pass.
    """
    import importlib

    module = importlib.import_module("openviking.storage.viking_vector_index_backend")
    backend = getattr(module, "VikingVectorIndexBackend", None)
    build_scope = getattr(backend, "_build_scope_filter", None)
    if build_scope is None or not callable(getattr(backend, "_acl_enabled", None)):
        raise RuntimeError(
            "VikingVectorIndexBackend lacks _build_scope_filter or _acl_enabled, "
            "which the keyword leg uses to stay inside the user's scope. This "
            "OpenViking is not one ov-ext supports."
        )
    params = inspect.signature(build_scope).parameters
    unknown = sorted(
        name
        for name, param in params.items()
        if param.default is inspect.Parameter.empty
        and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        and name not in _SCOPE_ARGUMENTS
    )
    takes_any = any(param.kind is param.VAR_KEYWORD for param in params.values())
    refused = [] if takes_any else sorted(_SCOPE_ARGUMENTS - set(params))
    if unknown or refused:
        raise RuntimeError(
            "VikingVectorIndexBackend._build_scope_filter no longer matches the "
            f"call the keyword leg makes (requires: {', '.join(unknown) or '-'}; "
            f"does not take: {', '.join(refused) or '-'}). This OpenViking is "
            "not one ov-ext supports."
        )


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
    logger.info("ov-ext: restored OpenViking's retriever")
