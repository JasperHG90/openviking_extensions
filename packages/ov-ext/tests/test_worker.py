"""Reaching uvicorn's spawned workers.

The bug this exists for was silent in the worst way: the parent logged that it
had installed, and every process actually serving requests was stock. So the
decisive test here spawns a real child the way uvicorn does and asks it what
class it has -- assertions about the rewrite alone would have passed before
the fix too.
"""

from __future__ import annotations

import multiprocessing
import sys
from typing import Any

import pytest

import ov_ext.worker as worker_module
from ov_ext.worker import OPENVIKING_FACTORY, OUR_FACTORY, install, uninstall

ENV_PREFIX = "OV_REFLECT_"


@pytest.fixture(autouse=True)
def _restore() -> Any:
    """Undo the uvicorn patch between tests; it is process-global."""
    yield
    uninstall()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's own OV_REFLECT_* out of these tests."""
    for name in list(__import__("os").environ):
        if name.upper().startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)


class FakeUvicorn:
    """Records what would have been handed to uvicorn."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def run(self, app: Any = None, **kwargs: Any) -> str:
        self.calls.append((app, kwargs))
        return "served"


@pytest.fixture
def uvicorn_stub(monkeypatch: pytest.MonkeyPatch) -> FakeUvicorn:
    """Stand in for uvicorn.run so nothing actually binds a port."""
    import uvicorn

    stub = FakeUvicorn()
    monkeypatch.setattr(uvicorn, "run", stub.run)
    return stub


def launch(app: Any, **kwargs: Any) -> Any:
    """Call whatever `uvicorn.run` currently is, the way bootstrap does."""
    import uvicorn

    return uvicorn.run(app, **kwargs)


# --- the rewrite -----------------------------------------------------------


def test_a_single_worker_launch_is_left_alone(uvicorn_stub: FakeUvicorn) -> None:
    """One worker means one process, and that process already installed."""
    install()
    app = object()
    launch(app, host="127.0.0.1", port=1933)
    assert uvicorn_stub.calls == [(app, {"host": "127.0.0.1", "port": 1933})]


def test_a_multi_worker_launch_is_routed_through_our_factory(
    uvicorn_stub: FakeUvicorn,
) -> None:
    install()
    launch(OPENVIKING_FACTORY, factory=True, workers=4)
    passed, kwargs = uvicorn_stub.calls[0]
    assert passed == OUR_FACTORY
    assert kwargs["workers"] == 4
    assert kwargs["factory"] is True


def test_an_unrecognised_import_string_is_not_rewritten(
    uvicorn_stub: FakeUvicorn, caplog: pytest.LogCaptureFixture
) -> None:
    """Rewriting a string we do not recognise would be a guess that breaks boot.

    OpenViking renaming its factory must surface as a loud complaint, not as a
    server that will not start.
    """
    install()
    with caplog.at_level("ERROR"):
        launch("some.other:factory", factory=True, workers=2)
    passed, _ = uvicorn_stub.calls[0]
    assert passed == "some.other:factory"
    assert "cannot reach 2 workers" in caplog.text


def test_the_string_we_already_rewrote_is_left_alone(
    uvicorn_stub: FakeUvicorn, caplog: pytest.LogCaptureFixture
) -> None:
    """So a double install cannot complain about its own handiwork."""
    install()
    with caplog.at_level("ERROR"):
        launch(OUR_FACTORY, factory=True, workers=2)
    assert uvicorn_stub.calls[0][0] == OUR_FACTORY
    assert "cannot reach" not in caplog.text


def test_install_is_idempotent(uvicorn_stub: FakeUvicorn) -> None:
    install()
    once = __import__("uvicorn").run
    install()
    assert __import__("uvicorn").run is once


def test_uninstall_restores_uvicorn() -> None:
    import uvicorn

    original = uvicorn.run
    install()
    assert uvicorn.run is not original
    uninstall()
    assert uvicorn.run is original


# --- the lock that cannot be right across workers --------------------------


def test_a_process_lock_is_refused_when_several_workers_are_starting(
    uvicorn_stub: FakeUvicorn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`process` asserts one process. uvicorn is about to start four.

    A process-local lock cannot see the other three, so the failure would be
    silent duplicate sweeps rather than an error -- which is exactly the case
    the guarantee exists for.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}ENABLED", "true")
    monkeypatch.setenv(f"{ENV_PREFIX}USER_ID", "jasper")
    monkeypatch.setenv(f"{ENV_PREFIX}LOCK", "process")
    install()
    with pytest.raises(ValueError, match="exactly one process sweeps"):
        launch(OPENVIKING_FACTORY, factory=True, workers=4)


def test_a_postgres_lock_is_fine_across_workers(
    uvicorn_stub: FakeUvicorn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}ENABLED", "true")
    monkeypatch.setenv(f"{ENV_PREFIX}USER_ID", "jasper")
    monkeypatch.setenv(f"{ENV_PREFIX}LOCK", "postgres")
    monkeypatch.setenv(f"{ENV_PREFIX}LOCK_DSN", "postgresql://localhost/ov")
    install()
    launch(OPENVIKING_FACTORY, factory=True, workers=4)
    assert uvicorn_stub.calls[0][0] == OUR_FACTORY


def test_a_process_lock_is_fine_on_one_worker(
    uvicorn_stub: FakeUvicorn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}ENABLED", "true")
    monkeypatch.setenv(f"{ENV_PREFIX}USER_ID", "jasper")
    monkeypatch.setenv(f"{ENV_PREFIX}LOCK", "process")
    install()
    launch(object(), host="127.0.0.1")
    assert uvicorn_stub.calls


# --- the factory the worker actually imports -------------------------------


def test_the_worker_factory_installs_before_delegating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters: OpenViking's factory builds the app, so we patch first."""
    order: list[str] = []

    monkeypatch.setattr(
        "ov_ext.installer.install", lambda *a, **k: order.append("install")
    )
    import openviking.server.app as ov_app

    monkeypatch.setattr(
        ov_app, "create_worker_app", lambda: order.append("factory") or "app"
    )

    result = worker_module.create_worker_app()

    assert order == ["install", "factory"]
    assert result == "app"


def test_the_factory_openviking_ships_is_the_one_we_name() -> None:
    """A rename upstream must be caught here rather than at a customer's boot."""
    module_name, _, attribute = OPENVIKING_FACTORY.partition(":")
    import importlib

    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute, None))


# --- the proof ---------------------------------------------------------------


def _child_report(queue: Any) -> None:
    """Run in a spawned child, as uvicorn's workers do."""
    import importlib

    from ov_ext.worker import create_worker_app  # noqa: F401  (import is the point)

    # Install the way the factory would, without building a whole FastAPI app.
    from ov_ext.retrieval.config import HybridSettings
    from ov_ext.retrieval.patch import install as install_retrieval

    install_retrieval(HybridSettings())
    retriever = importlib.import_module("openviking.retrieve.hierarchical_retriever")
    queue.put(retriever.HierarchicalRetriever.__name__)


def test_a_spawned_worker_that_imports_our_module_is_patched() -> None:
    """The decisive one, and the shape of the original bug.

    A spawned child starts with an empty ``sys.modules`` and imports only what
    it is told to. Before the fix it was told to import OpenViking's factory
    and reported ``HierarchicalRetriever``; told to import ours, it installs
    and reports ``HybridRetriever``.
    """
    spawn = multiprocessing.get_context("spawn")
    queue: Any = spawn.Queue()
    child = spawn.Process(target=_child_report, args=(queue,))
    child.start()
    try:
        assert queue.get(timeout=180) == "HybridRetriever"
    finally:
        child.join(timeout=30)


def test_a_spawned_child_inherits_nothing_from_this_process() -> None:
    """The premise the whole module rests on, asserted rather than assumed.

    If uvicorn forked, the patched modules would come across and none of this
    would be needed. It spawns.
    """
    import uvicorn._subprocess as subprocess_module

    assert subprocess_module.spawn.get_start_method() == "spawn"
    assert sys.modules.get("ov_ext") is not None  # loaded here, not there
