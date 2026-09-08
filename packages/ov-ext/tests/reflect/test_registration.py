"""Making the observations memory type visible to OpenViking.

The awkward part is that OpenViking has one custom-templates *directory*
setting, and it may already belong to someone. Getting this wrong silently
removes memory types a deployment relies on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import ov_ext.reflect.registration as register_module
from ov_ext.reflect.config import ReflectSettings
from ov_ext.reflect.registration import TEMPLATES_DIR, register, unregister


class FakeConfig:
    """Stands in for OpenViking's config object, which is mutated in place."""

    def __init__(self, custom_templates_dir: str = "") -> None:
        self.memory = type("memory", (), {"custom_templates_dir": custom_templates_dir})()


@pytest.fixture(autouse=True)
def _reset() -> Any:
    """Clear the module's state between tests; it is process-global."""
    register_module._installed = None
    register_module._previous_dir = register_module._UNSET
    yield
    register_module._installed = None
    register_module._previous_dir = register_module._UNSET


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> FakeConfig:
    """Install a fake OpenViking config both helpers resolve through."""
    fake = FakeConfig()

    def get_openviking_config() -> FakeConfig:
        return fake

    module = type("m", (), {"get_openviking_config": staticmethod(get_openviking_config)})
    monkeypatch.setitem(
        __import__("sys").modules, "openviking_cli.utils.config", module  # type: ignore[arg-type]
    )
    return fake


def enabled(**overrides: Any) -> ReflectSettings:
    base = ReflectSettings().model_dump()
    base.update({"enabled": True})
    base.update(overrides)
    return ReflectSettings.model_construct(**base)


def test_the_schema_on_disk_is_a_valid_memory_type() -> None:
    """It has to parse the way OpenViking's own templates do, or it is ignored."""
    schema = yaml.safe_load((TEMPLATES_DIR / "observations.yaml").read_text())
    assert schema["memory_type"] == "observations"
    assert schema["enabled"] is True
    names = {field["name"] for field in schema["fields"]}
    # The two the filename_template interpolates, plus the body.
    assert {"topic", "name", "content"} <= names
    assert "{{ topic|lower }}/{{ name|lower }}.md" == schema["filename_template"]


def test_disabled_reflection_registers_nothing(config: FakeConfig) -> None:
    """A memory type nothing writes is clutter in someone's schema listing."""
    register(ReflectSettings.model_construct(**ReflectSettings().model_dump()))
    assert config.memory.custom_templates_dir == ""


def test_an_unset_directory_is_pointed_at_ours(config: FakeConfig) -> None:
    register(enabled())
    assert config.memory.custom_templates_dir == str(TEMPLATES_DIR)


def test_unregister_puts_back_what_was_there(config: FakeConfig) -> None:
    """Leaving ours in place would suppress OpenViking's own fallback."""
    register(enabled())
    assert config.memory.custom_templates_dir == str(TEMPLATES_DIR)

    unregister()

    assert config.memory.custom_templates_dir == ""


def test_someone_elses_directory_is_not_taken_over(
    config: FakeConfig, tmp_path: Path
) -> None:
    """Redirecting it would silently drop every memory type it holds."""
    theirs = tmp_path / "their-templates"
    theirs.mkdir()
    config.memory.custom_templates_dir = str(theirs)

    register(enabled())

    assert config.memory.custom_templates_dir == str(theirs)
    assert (theirs / "observations.yaml").exists()


def test_unregister_leaves_their_directory_otherwise_untouched(
    config: FakeConfig, tmp_path: Path
) -> None:
    theirs = tmp_path / "their-templates"
    theirs.mkdir()
    (theirs / "their_type.yaml").write_text("memory_type: theirs\n")
    config.memory.custom_templates_dir = str(theirs)

    register(enabled())
    unregister()

    assert not (theirs / "observations.yaml").exists()
    assert (theirs / "their_type.yaml").exists()
    assert config.memory.custom_templates_dir == str(theirs)


def test_registering_without_an_openviking_config_does_not_raise() -> None:
    """A CLI or a test that never booted a server still has to import cleanly."""
    register(enabled())
    unregister()
