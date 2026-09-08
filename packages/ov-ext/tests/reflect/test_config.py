"""Settings, and the guard that refuses a variable nobody reads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ov_ext.reflect.config import ENV_PREFIX, ReflectSettings
from ov_ext.reflect.prompts import propose_prompt

pytestmark = pytest.mark.usefixtures("clean_env")


def test_reflection_is_off_until_someone_turns_it_on() -> None:
    """It writes to memory unattended; that should be a decision, not a default."""
    assert ReflectSettings().enabled is False


def test_the_safeguards_are_on_by_default() -> None:
    settings = ReflectSettings()
    assert settings.tail_sample > 0  # or reflection only confirms itself
    assert settings.min_evidence >= 2  # or one memory restated is an "observation"
    assert settings.require_cross_peer is False  # single-project ones are wanted too


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}ENABLED", "true")
    monkeypatch.setenv(f"{ENV_PREFIX}MIN_EVIDENCE", "5")
    settings = ReflectSettings()
    assert settings.enabled is True
    assert settings.min_evidence == 5


def test_a_misspelled_variable_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-settings never enumerates the environment, so it would be ignored.

    Someone who set it would watch reflection ignore them with nothing to
    explain why.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}MIN_EVIDNCE", "5")
    with pytest.raises(ValidationError, match="MIN_EVIDNCE"):
        ReflectSettings()


def test_an_out_of_range_value_is_refused_rather_than_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}MIN_EVIDENCE", "0")
    with pytest.raises(ValidationError):
        ReflectSettings()


def test_the_prompt_tells_the_model_not_to_cite_the_overview() -> None:
    """The overview is model-written; a quote found in it proves only that it said so."""
    prompt = propose_prompt([], scope="Some generated summary.")
    assert "never quote or cite it" in prompt
    assert "Some generated summary." in prompt


def test_the_prompt_omits_the_background_section_when_there_is_none() -> None:
    assert "Background on the area" not in propose_prompt([])
