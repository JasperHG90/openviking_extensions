"""Tests for configuration and credential loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ov_sync.config import (
    CONFIG_FILENAME,
    Credentials,
    cli_config_path,
    find_config_file,
    load_config,
    load_credentials,
)


def test_defaults_apply_without_a_config_file(folder: Path) -> None:
    """A folder with no config still loads, minus a target."""
    config = load_config(folder)

    assert config.sync.root_uri is None
    assert config.sync.state_file == ".ov-sync.db"
    assert config.watch.mode == "events"


def test_config_file_in_the_folder_is_read(folder: Path) -> None:
    """ov-sync.toml beside the notes wins over the defaults."""
    (folder / CONFIG_FILENAME).write_text(
        '[sync]\nroot_uri = "viking://resources/notes"\nmax_operations_per_batch = 50\n',
        encoding="utf-8",
    )

    config = load_config(folder)

    assert config.sync.root_uri == "viking://resources/notes"
    assert config.sync.max_operations_per_batch == 50


def test_environment_beats_the_config_file(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env var overrides the file, which is what makes CI overrides work."""
    (folder / CONFIG_FILENAME).write_text(
        '[sync]\nroot_uri = "viking://resources/from-file"\n', encoding="utf-8"
    )
    monkeypatch.setenv("OV_SYNC_SYNC__ROOT_URI", "viking://resources/from-env")

    config = load_config(folder)

    assert config.sync.root_uri == "viking://resources/from-env"


def test_an_unknown_key_is_rejected(folder: Path) -> None:
    """A typo'd setting fails loudly instead of being silently ignored."""
    (folder / CONFIG_FILENAME).write_text('[sync]\nroot_url = "oops"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="root_url"):
        load_config(folder)


def test_malformed_toml_names_the_file(folder: Path) -> None:
    """The error says which file to go and fix."""
    (folder / CONFIG_FILENAME).write_text("[sync\n", encoding="utf-8")

    with pytest.raises(ValueError, match=CONFIG_FILENAME):
        load_config(folder)


def test_an_explicit_config_path_must_exist(folder: Path) -> None:
    """Falling back silently would sync with settings nobody asked for."""
    with pytest.raises(FileNotFoundError):
        find_config_file(folder, folder / "nope.toml")


def test_the_global_config_is_the_fallback(folder: Path, tmp_path: Path) -> None:
    """A config under XDG_CONFIG_HOME covers folders that carry none."""
    global_config = tmp_path / "home" / ".config" / "ov-sync" / CONFIG_FILENAME
    global_config.parent.mkdir(parents=True)
    global_config.write_text('[sync]\nstate_file = ".custom.db"\n', encoding="utf-8")

    assert find_config_file(folder) == global_config
    assert load_config(folder).sync.state_file == ".custom.db"


def test_extensions_are_normalized(folder: Path) -> None:
    """A bare or upper-case extension still matches what the scanner sees."""
    (folder / CONFIG_FILENAME).write_text(
        '[sync]\ninclude_extensions = ["MD", ".TXT"]\n', encoding="utf-8"
    )

    assert load_config(folder).sync.include_extensions == [".md", ".txt"]


def test_credentials_come_from_the_ov_cli_config(tmp_path: Path) -> None:
    """Ovsync reuses whatever `ov config` already wrote."""
    config_file = tmp_path / "ovcli.conf"
    config_file.write_text(
        json.dumps(
            {
                "url": "https://ov.example/",
                "api_key": "k",
                "account": "lab",
                "user": "jasper",
            }
        ),
        encoding="utf-8",
    )

    credentials = load_credentials(config_file)

    assert credentials.url == "https://ov.example"
    assert credentials.api_key == "k"
    assert credentials.account == "lab"


def test_environment_overrides_the_credential_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key can stay out of the file entirely."""
    config_file = tmp_path / "ovcli.conf"
    config_file.write_text(json.dumps({"url": "https://ov.example", "api_key": "k"}))
    monkeypatch.setenv("OV_SYNC_API_KEY", "from-env")

    assert load_credentials(config_file).api_key == "from-env"


def test_missing_credentials_say_how_to_fix_it(tmp_path: Path) -> None:
    """The error names the command that creates the file."""
    with pytest.raises(FileNotFoundError, match="ov config"):
        load_credentials(tmp_path / "absent.conf")


def test_credentials_from_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No file is needed when the environment supplies both required fields."""
    monkeypatch.setenv("OV_SYNC_URL", "https://ov.example")
    monkeypatch.setenv("OV_SYNC_API_KEY", "k")

    assert load_credentials(tmp_path / "absent.conf").url == "https://ov.example"


def test_an_incomplete_credential_file_is_rejected(tmp_path: Path) -> None:
    """A config with a URL but no key cannot authenticate, so it fails early."""
    config_file = tmp_path / "ovcli.conf"
    config_file.write_text(json.dumps({"url": "https://ov.example"}), encoding="utf-8")

    with pytest.raises(ValueError, match="no url or api_key"):
        load_credentials(config_file)


def test_the_cli_config_path_honors_the_ov_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ovx points `ov` at a per-profile config; ovsync follows it."""
    monkeypatch.setenv("OPENVIKING_CLI_CONFIG_FILE", "/tmp/profile.conf")

    assert cli_config_path() == Path("/tmp/profile.conf")


def test_credentials_strip_a_trailing_slash() -> None:
    """Paths are joined onto the URL, so a trailing slash would double up."""
    assert Credentials(url="https://ov.example/", api_key="k").url == "https://ov.example"
