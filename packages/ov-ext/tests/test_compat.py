"""The startup check against an untested OpenViking. No database."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from importlib.metadata import PackageNotFoundError

from ov_ext import compat

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test without the override, whatever the shell has set."""
    monkeypatch.delenv(compat.ALLOW_UNTESTED_ENV, raising=False)


def installed(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Make the check see ``value`` as the installed OpenViking version."""

    def fake_version(name: str) -> str:
        assert name == "openviking"
        if value is None:
            raise PackageNotFoundError(name)
        return value

    monkeypatch.setattr(compat, "version", fake_version)


def check() -> None:
    """Run the check the way ``ov_ext.install()`` does, first thing."""
    compat.check_openviking_version()


def test_the_range_matches_the_pyproject_requirement() -> None:
    """The runtime range and the install-time cap must say the same thing."""
    requirement = re.search(r'"openviking([<>=!~][^"]*)"', PYPROJECT.read_text())
    assert requirement is not None
    low = ".".join(map(str, compat.OPENVIKING_MIN))
    high = ".".join(map(str, compat.OPENVIKING_BELOW))
    assert requirement.group(1) == f">={low},<{high}"


@pytest.mark.parametrize(
    "value", ["0.5.6", "0.5.9", "0.5.6.1", "0.5.6.post1", "0.5.6+local", "0.5.10rc1"]
)
def test_a_tested_version_starts_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    """In range, the check passes and nothing is logged."""
    installed(monkeypatch, value)
    with caplog.at_level(logging.WARNING, logger="ov_ext.compat"):
        check()
    assert caplog.records == []


@pytest.mark.parametrize(
    "value",
    [
        "0.4.17",
        "0.5.5",
        "0.5.6rc1",
        "0.5.6.dev1",
        "0.5.6a2",
        "0.6.0",
        "0.6.0rc1",
        "1.0.0",
    ],
)
def test_an_untested_version_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Out of range, the check raises naming both versions."""
    installed(monkeypatch, value)
    with pytest.raises(RuntimeError, match=re.escape(value)) as raised:
        check()
    assert ">=0.5.6,<0.6" in str(raised.value)


def test_a_missing_openviking_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """No readable version is not a pass."""
    installed(monkeypatch, None)
    with pytest.raises(RuntimeError, match="unknown"):
        check()


def test_the_override_downgrades_the_refusal_to_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deliberate trial on an untested version stays possible."""
    installed(monkeypatch, "0.6.0")
    monkeypatch.setenv(compat.ALLOW_UNTESTED_ENV, "1")
    with caplog.at_level(logging.WARNING, logger="ov_ext.compat"):
        check()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "0.6.0" in caplog.records[0].getMessage()


def test_a_garbled_override_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value that is not a boolean is an error, not a silent no."""
    installed(monkeypatch, "0.6.0")
    monkeypatch.setenv(compat.ALLOW_UNTESTED_ENV, "maybe")
    with pytest.raises(ValueError):
        check()


def test_install_runs_the_check_before_patching_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ov_ext.install()`` refuses an untested OpenViking with nothing patched."""
    import importlib

    import ov_ext

    installed(monkeypatch, "0.6.0")
    module = importlib.import_module("openviking.retrieve.hierarchical_retriever")
    original = module.HierarchicalRetriever
    with pytest.raises(RuntimeError, match="0.6.0"):
        ov_ext.install()
    assert module.HierarchicalRetriever is original
