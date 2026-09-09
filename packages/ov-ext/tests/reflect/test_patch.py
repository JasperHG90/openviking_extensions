"""Starting the ticker with OpenViking's service.

This module wraps a method on somebody else's class during their startup. It
shipped untested, and two defects lived in the gap: the ticker read the
environment again instead of the settings it was handed, so a caller who
configured reflection in code got a ticker that logged success and never ran;
and a ``user_id`` OpenViking rejects escaped as an exception that aborted the
whole boot.

Both are covered below, against the real ``OpenVikingService`` where it
matters -- a fake service would have agreed with either bug.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import ov_ext.reflect.patch as patch_module
from ov_ext.reflect.config import LockKind, ReflectSettings
from ov_ext.reflect.patch import install, uninstall

pytestmark = pytest.mark.usefixtures("clean_env")

_MODULE = "openviking.service.core"
_CLASS = "OpenVikingService"


@pytest.fixture(autouse=True)
def _restore() -> Any:
    """Undo the patch between tests; it is process-global."""
    yield
    uninstall()
    patch_module._original = None
    patch_module._task = None


def service_class() -> Any:
    """The real class the patch wraps."""
    import importlib

    return getattr(importlib.import_module(_MODULE), _CLASS)


def settings(**overrides: Any) -> ReflectSettings:
    """Enabled settings that never consult the environment."""
    base = ReflectSettings().model_dump()
    base.update(
        {
            "enabled": True,
            "user_id": "jasper",
            "lock": LockKind.PROCESS,
            "interval_seconds": 3600,
        }
    )
    base.update(overrides)
    return ReflectSettings.model_construct(**base)


class FakeService:
    """Enough of OpenVikingService for the wrapper to work with."""

    def __init__(self, *, complete: bool = True) -> None:
        self.viking_fs = object() if complete else None
        self.vikingdb_manager = object() if complete else None
        self.initialized = False


@pytest.fixture
def stub_initialize(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the real `initialize` with a stub, before the patch wraps it.

    The wrapper delegates to whatever it displaced, and the real method boots a
    whole service. Stubbing first means these tests exercise the wrapper on the
    real class -- the thing that has been wrong twice -- without booting
    OpenViking.
    """
    called: list[Any] = []

    async def stub(service: Any, *args: Any, **kwargs: Any) -> str:
        called.append(service)
        service.initialized = True
        return "ok"

    monkeypatch.setattr(service_class(), "initialize", stub)
    return called


# --- what the patch assumes about OpenViking -------------------------------


def test_the_method_being_wrapped_exists_and_is_async() -> None:
    """A rename upstream must surface here, not as reflection never running."""
    import inspect

    method = getattr(service_class(), "initialize", None)
    assert method is not None and inspect.iscoroutinefunction(method)


def test_the_service_exposes_what_the_ticker_needs() -> None:
    """`viking_fs` and `vikingdb_manager` are the two the ticker reads."""
    cls = service_class()
    assert isinstance(getattr(cls, "viking_fs", None), property)
    assert isinstance(getattr(cls, "vikingdb_manager", None), property)


def test_install_refuses_when_openviking_has_moved_the_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better a refused startup than a subsystem that silently never runs."""
    monkeypatch.delattr(service_class(), "initialize", raising=True)
    with pytest.raises(RuntimeError, match="initialize"):
        install(settings())


# --- installing and uninstalling -------------------------------------------


def test_a_disabled_install_leaves_the_class_alone() -> None:
    original = service_class().initialize
    install(ReflectSettings.model_construct(**ReflectSettings().model_dump()))
    assert service_class().initialize is original


def test_install_refuses_without_a_lock() -> None:
    """The guarantee, enforced at startup rather than at the first tick."""
    with pytest.raises(ValueError, match="LOCK is not set"):
        install(settings(lock=LockKind.UNSET))
    # And it must not have wrapped anything on the way out.
    assert patch_module._original is None


def test_install_is_idempotent() -> None:
    install(settings())
    once = service_class().initialize
    install(settings())
    assert service_class().initialize is once


def test_uninstall_puts_the_original_back() -> None:
    original = service_class().initialize
    install(settings())
    assert service_class().initialize is not original
    uninstall()
    assert service_class().initialize is original


def test_uninstall_without_install_is_harmless() -> None:
    original = service_class().initialize
    uninstall()
    assert service_class().initialize is original


# --- what happens at boot ---------------------------------------------------


async def test_the_ticker_starts_and_the_original_still_runs(
    monkeypatch: pytest.MonkeyPatch, stub_initialize: list[Any]
) -> None:
    started: list[Any] = []

    async def fake_ticker(*args: Any, **kwargs: Any) -> None:
        started.append(args)
        await asyncio.sleep(3600)

    monkeypatch.setattr("ov_ext.reflect.patch.run_ticker", fake_ticker)
    install(settings())
    service = FakeService()

    result = await service_class().initialize(service)
    await asyncio.sleep(0)

    assert result == "ok", "the wrapped method's return value must pass through"
    assert stub_initialize == [service], "OpenViking's own initialize must still run"
    assert started, "the ticker should have been started"


async def test_settings_passed_in_code_are_the_ones_used(
    monkeypatch: pytest.MonkeyPatch, stub_initialize: list[Any]
) -> None:
    """The defect that shipped: the ticker re-read an empty environment.

    With no OV_REFLECT_* set, a caller passing settings in code got a ticker
    that logged that it had started and then silently did not, because the
    user it looked up came from the environment rather than the argument.
    """
    seen: list[Any] = []

    async def fake_ticker(fs: Any, db: Any, ctx: Any, lock: Any, cfg: Any) -> None:
        seen.append(ctx)
        await asyncio.sleep(3600)

    monkeypatch.setattr("ov_ext.reflect.patch.run_ticker", fake_ticker)
    install(settings(user_id="from_code"))
    await service_class().initialize(FakeService())
    await asyncio.sleep(0)

    assert seen, "the ticker must start from settings passed in code"
    assert seen[0].user.user_id == "from_code"


async def test_a_service_missing_its_dependencies_does_not_start_a_ticker(
    monkeypatch: pytest.MonkeyPatch, stub_initialize: list[Any]
) -> None:
    async def fake_ticker(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("should not have started")

    monkeypatch.setattr("ov_ext.reflect.patch.run_ticker", fake_ticker)
    install(settings())
    await service_class().initialize(FakeService(complete=False))
    await asyncio.sleep(0)
    assert patch_module._task is None


async def test_a_context_openviking_rejects_does_not_abort_the_boot(
    monkeypatch: pytest.MonkeyPatch, stub_initialize: list[Any]
) -> None:
    """The second shipped defect: an invalid user_id killed the server.

    UserIdentifier validates its argument and raises ValueError, which escaped
    an ImportError-only guard, propagated out of the wrapper, and took
    OpenViking's startup with it. Config refuses such a value now, but the
    boot path must survive one regardless -- a server that cannot reflect
    should still serve.
    """

    def exploding_identifier(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("user_id must be alpha_numeric string.")

    monkeypatch.setattr(
        "openviking.server.identity.UserIdentifier", exploding_identifier
    )
    monkeypatch.setattr("ov_ext.reflect.patch.run_ticker", _never)
    install(settings())

    service = FakeService()
    await service_class().initialize(service)

    assert service.initialized is True, "OpenViking's own startup must complete"
    assert patch_module._task is None


async def _never(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
    """A ticker that should never be reached."""
    raise AssertionError("should not have started")
