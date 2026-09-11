"""Shared fixtures.

Every test runs against a temporary folder and a temporary state database. No
test reads the real ``~/.openviking`` or the real config directory: the
``isolate_environment`` fixture points both somewhere disposable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ov_sync.config import Credentials, OvSyncConfig, SyncConfig

OV_ENV_VARS = (
    "OV_SYNC_URL",
    "OV_SYNC_API_KEY",
    "OV_SYNC_ACCOUNT",
    "OV_SYNC_USER",
    "OV_SYNC_SYNC__ROOT_URI",
    "OPENVIKING_CLI_CONFIG_FILE",
    "XDG_CONFIG_HOME",
)


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from the real home directory and the real env.

    Without this, a developer's own ``~/.openviking/ovcli.conf`` would leak
    into the credential tests, and a stray ``OV_SYNC_*`` in their shell would
    silently change what the config tests assert.
    """
    for name in OV_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """An empty folder to sync."""
    path = tmp_path / "notes"
    path.mkdir()
    return path


@pytest.fixture
def sync_config() -> SyncConfig:
    """A sync configuration with predictable defaults."""
    return SyncConfig(root_uri="viking://resources/notes")


@pytest.fixture
def full_config(sync_config: SyncConfig) -> OvSyncConfig:
    """A whole configuration built around ``sync_config``."""
    return OvSyncConfig(sync=sync_config)


@pytest.fixture
def credentials() -> Credentials:
    """Credentials pointing at a server respx will intercept."""
    return Credentials(
        url="https://openviking.example",
        api_key="test-key",
        account="acme",
        user="jasper",
    )
