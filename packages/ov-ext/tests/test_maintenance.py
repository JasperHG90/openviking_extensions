"""The maintenance task that runs backend upkeep beside the started server."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from ov_ext import maintenance

pytestmark = pytest.mark.asyncio


class RecordingAdapter:
    """An adapter whose md5 backfill records the filesystem it was handed."""

    def __init__(self, filled: int = 0, error: Exception | None = None) -> None:
        self.calls: list[Any] = []
        self._filled = filled
        self._error = error

    async def run_md5_backfill(self, viking_fs: Any) -> int:
        self.calls.append(viking_fs)
        if self._error is not None:
            raise self._error
        return self._filled


@pytest.fixture(autouse=True)
def restore_service() -> Iterator[None]:
    """Leave OpenViking's service class as it was."""
    from openviking.service.core import OpenVikingService

    original = OpenVikingService.initialize
    yield
    maintenance.uninstall()
    OpenVikingService.initialize = original  # type: ignore[method-assign]


async def test_every_reachable_adapter_is_backfilled_once() -> None:
    """The shared adapter and each account's dedicated one; shared ones once."""
    shared, dedicated = RecordingAdapter(filled=3), RecordingAdapter(filled=2)
    store = SimpleNamespace(
        _shared_adapter=shared,
        _resolved_backends={
            "acme": SimpleNamespace(_adapter=dedicated),
            "default": SimpleNamespace(_adapter=shared),
        },
    )
    filled = await maintenance.run_once(store, "fs")
    assert filled == 5
    assert shared.calls == ["fs"] and dedicated.calls == ["fs"]


async def test_one_failing_adapter_does_not_stop_the_rest() -> None:
    """A failure is logged; the next adapter still runs."""
    broken, healthy = RecordingAdapter(error=RuntimeError("db down")), RecordingAdapter(1)
    store = SimpleNamespace(
        _shared_adapter=broken,
        _resolved_backends={"acme": SimpleNamespace(_adapter=healthy)},
    )
    assert await maintenance.run_once(store, "fs") == 1
    assert healthy.calls == ["fs"]


async def test_an_adapter_without_maintenance_is_left_alone() -> None:
    """Other backends cost nothing."""
    store = SimpleNamespace(_shared_adapter=object(), _resolved_backends={})
    assert await maintenance.run_once(store, "fs") == 0


async def test_the_backfill_starts_when_the_service_has_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed, OpenViking's own initialize ends by starting the backfill."""
    from openviking.service.core import OpenVikingService

    async def started(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(OpenVikingService, "initialize", started)
    adapter = RecordingAdapter()
    service = SimpleNamespace(
        vikingdb_manager=SimpleNamespace(_shared_adapter=adapter, _resolved_backends={}),
        viking_fs="the server's fs",
    )
    maintenance.install()
    await OpenVikingService.initialize(service)  # type: ignore[arg-type]
    for _ in range(10):
        if adapter.calls:
            break
        await asyncio.sleep(0)
    assert adapter.calls == ["the server's fs"]


async def test_ov_ext_install_includes_the_maintenance_task() -> None:
    """The one entry point installs it, and uninstall removes it again."""
    import ov_ext
    from openviking.service.core import OpenVikingService

    original = OpenVikingService.initialize
    ov_ext.install()
    try:
        assert OpenVikingService.initialize is not original
        assert maintenance._original is not None
    finally:
        ov_ext.uninstall()
    assert OpenVikingService.initialize is original
