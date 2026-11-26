"""Tests for the monkeypatch that puts the hybrid retriever in front.

These touch process-global state -- the attribute on OpenViking's module -- so
every test restores it, and the autouse fixture is the backstop for the ones
that fail partway.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

from ov_ext.retrieval import patch as install_module
from ov_ext.retrieval.patch import install, uninstall
from ov_ext.retrieval.retriever import HybridRetriever

MODULE = "openviking.retrieve.hierarchical_retriever"
ATTRIBUTE = "HierarchicalRetriever"


@pytest.fixture(autouse=True)
def restore_openviking() -> Iterator[None]:
    """Undo any patch, however the test ended."""
    module = importlib.import_module(MODULE)
    original = getattr(module, ATTRIBUTE)
    yield
    setattr(module, ATTRIBUTE, original)
    install_module._original = None


def current() -> type:
    """Return whatever OpenViking would build a retriever from right now."""
    return getattr(importlib.import_module(MODULE), ATTRIBUTE)  # type: ignore[no-any-return]


def test_install_puts_the_hybrid_retriever_in_place() -> None:
    """Both of OpenViking's call sites resolve this name at call time."""
    install()

    assert issubclass(current(), HybridRetriever)


def test_installing_twice_does_not_stack() -> None:
    """A second call must not wrap the first, or uninstall could never undo it."""
    install()
    first = current()
    install()

    assert current() is first


def test_uninstall_restores_openvikings_own_class() -> None:
    """Leaving a patch behind would change every later search in the process."""
    before = current()
    install()
    uninstall()

    assert current() is before


def test_uninstall_after_a_double_install_still_restores() -> None:
    """The regression that identity-checking the subclass caused.

    ``install`` binds a subclass of ``HybridRetriever``, so a second call that
    compared with ``is`` saw an unfamiliar class, recorded *its own* patch as
    the original, and left ``uninstall`` restoring a patch.
    """
    before = current()
    install()
    install()
    uninstall()

    assert current() is before


def test_uninstall_without_install_is_harmless() -> None:
    """Startup and shutdown paths should not have to track each other."""
    before = current()
    uninstall()

    assert current() is before


def test_a_missing_upstream_class_is_refused_loudly() -> None:
    """Silently skipping the patch would lose retrieval quality months later."""
    module = importlib.import_module(MODULE)
    delattr(module, ATTRIBUTE)

    with pytest.raises(RuntimeError, match="does not lay out its retriever"):
        install()


def test_a_non_class_upstream_is_refused() -> None:
    """Subclassing something that is not a class fails far from the cause."""
    module = importlib.import_module(MODULE)
    setattr(module, ATTRIBUTE, "not a class")

    with pytest.raises(RuntimeError, match="not a class"):
        install()


def test_install_accepts_this_openvikings_scope_builder() -> None:
    """Against the OpenViking the suite runs on, the contract check passes."""
    install()
    assert issubclass(current(), HybridRetriever)
    uninstall()


@pytest.mark.parametrize(
    "replacement,complaint",
    [
        (
            lambda self,
            ctx,
            context_type,
            target_directories,
            extra_filter,
            level=None,
            *,
            acl_enabled,
            tier: None,  # noqa: E501
            "requires: tier",
        ),
        (
            lambda self,
            ctx,
            context_type,
            target_directories,
            extra_filter,
            level=None: None,  # noqa: E501
            "does not take: acl_enabled",
        ),
    ],
)
def test_a_scope_builder_ov_ext_cannot_call_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, replacement: object, complaint: str
) -> None:
    """A changed ``_build_scope_filter`` stops startup, and patches nothing.

    At query time the keyword leg would catch the TypeError and fall back to
    vector-only on every search, which is how ov-ext 0.7 lost its keyword leg
    on OpenViking 0.5 without anyone noticing.
    """
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

    original = current()
    monkeypatch.setattr(VikingVectorIndexBackend, "_build_scope_filter", replacement)
    with pytest.raises(RuntimeError, match=complaint):
        install()
    assert current() is original


def test_a_backend_without_acl_state_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_acl_enabled`` is where the keyword leg's ACL flag comes from."""
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

    monkeypatch.delattr(VikingVectorIndexBackend, "_acl_enabled")
    with pytest.raises(RuntimeError, match="_acl_enabled"):
        install()
