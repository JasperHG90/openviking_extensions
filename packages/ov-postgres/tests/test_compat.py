"""The startup check against an untested OpenViking. No database."""

from __future__ import annotations

import logging
import re
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

from ov_postgres import compat
from ov_postgres.adapter import PgVectorCollectionAdapter

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


def build() -> PgVectorCollectionAdapter:
    """Build an unconnected adapter the way OpenViking does."""
    config = VectorDBBackendConfig(
        backend="ov_postgres.adapter.PgVectorCollectionAdapter",
        name="context",
        custom_params={"dsn": "postgresql://user:pw@localhost:5432/openviking"},
    )
    return PgVectorCollectionAdapter.from_config(config)


def test_the_range_matches_the_pyproject_requirement() -> None:
    """The runtime range and the install-time cap must say the same thing."""
    requirement = re.search(r'"openviking([<>=!~][^"]*)"', PYPROJECT.read_text())
    assert requirement is not None
    low = ".".join(map(str, compat.OPENVIKING_MIN))
    high = ".".join(map(str, compat.OPENVIKING_BELOW))
    assert requirement.group(1) == f">={low},<{high}"


@pytest.mark.parametrize(
    "value",
    [
        "0.5.6",
        "0.5.9",
        "0.5.6.1",
        "0.5.6.post1",
        "0.5.6.post1.dev2",
        "0.5.6+local",
        "0.5.10rc1",
    ],
)
def test_a_tested_version_starts_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    """In range, the adapter builds and nothing is logged."""
    installed(monkeypatch, value)
    with caplog.at_level(logging.WARNING, logger="ov_postgres.compat"):
        build()
    assert caplog.records == []


@pytest.mark.parametrize(
    "value",
    [
        "0.4.17",
        "0.5.5",
        "0.5.6rc1",
        "0.5.6.dev1",
        "0.5.6a2",
        "0.5.6rc1.post1",
        "0.6.0",
        "0.6.0rc1",
        "1.0.0",
    ],
)
def test_an_untested_version_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Out of range, ``from_config`` raises naming both versions."""
    installed(monkeypatch, value)
    with pytest.raises(RuntimeError, match=re.escape(value)) as raised:
        build()
    assert ">=0.5.6,<0.6" in str(raised.value)


def test_a_missing_openviking_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """No readable version is not a pass."""
    installed(monkeypatch, None)
    with pytest.raises(RuntimeError, match="unknown"):
        build()


def test_the_override_downgrades_the_refusal_to_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deliberate trial on an untested version stays possible."""
    installed(monkeypatch, "0.6.0")
    monkeypatch.setenv(compat.ALLOW_UNTESTED_ENV, "1")
    with caplog.at_level(logging.WARNING, logger="ov_postgres.compat"):
        build()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "0.6.0" in caplog.records[0].getMessage()


def test_a_garbled_override_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value that is not a boolean is an error, not a silent no."""
    installed(monkeypatch, "0.6.0")
    monkeypatch.setenv(compat.ALLOW_UNTESTED_ENV, "maybe")
    with pytest.raises(ValueError):
        build()
