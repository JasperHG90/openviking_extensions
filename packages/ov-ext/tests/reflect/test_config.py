"""Settings, and the guard that refuses a variable nobody reads."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from ov_ext.reflect.config import ENV_PREFIX, RETIRED, ReflectSettings
from ov_ext.reflect.prompts import propose_prompt

pytestmark = pytest.mark.usefixtures("clean_env")


def test_reflection_is_off_until_someone_turns_it_on() -> None:
    """It writes to memory unattended; that should be a decision, not a default."""
    assert ReflectSettings().enabled is False


def test_the_safeguards_are_on_by_default() -> None:
    settings = ReflectSettings()
    assert settings.tail_sample > 0  # or reflection only confirms itself
    assert settings.min_evidence >= 2  # or one memory restated is an "observation"
    assert settings.require_cross_area is False  # single-project ones are wanted too


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(f"{ENV_PREFIX}ENABLED", "true")
    # Enabling reflection requires naming whose memories it reads; it serves no
    # request, so it has no user to inherit.
    monkeypatch.setenv(f"{ENV_PREFIX}USER_ID", "jasper")
    monkeypatch.setenv(f"{ENV_PREFIX}MIN_EVIDENCE", "5")
    settings = ReflectSettings()
    assert settings.enabled is True
    assert settings.user_id == "jasper"
    assert settings.min_evidence == 5


def test_a_misspelled_variable_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pydantic-settings never enumerates the environment, so it would be ignored.

    Someone who set it would watch reflection ignore them with nothing to
    explain why.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}MIN_EVIDNCE", "5")
    with pytest.raises(ValidationError, match="MIN_EVIDNCE"):
        ReflectSettings()


def test_a_retired_variable_is_tolerated_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deleted setting must not stop a server that still carries it.

    This validator runs inside `ov_ext.install()`, which is fatal by design, so
    refusing a retired name would take retrieval down with reflection over a
    line in a config file that no longer does anything.
    """
    retired = next(iter(RETIRED))
    monkeypatch.setenv(retired, "true")

    with caplog.at_level(logging.WARNING):
        ReflectSettings()

    assert retired in caplog.text
    assert "no longer does anything" in caplog.text


def test_a_retired_variable_does_not_excuse_a_misspelled_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tolerating one must not turn the misspelling guard off for the rest."""
    monkeypatch.setenv(next(iter(RETIRED)), "true")
    monkeypatch.setenv(f"{ENV_PREFIX}MIN_EVIDNCE", "5")

    with pytest.raises(ValidationError, match="MIN_EVIDNCE"):
        ReflectSettings()


def test_a_retired_variable_is_matched_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Environments are not consistent about case, and a miss here is a dead server."""
    monkeypatch.setenv(next(iter(RETIRED)).lower(), "true")

    with caplog.at_level(logging.WARNING):
        ReflectSettings()

    assert "no longer does anything" in caplog.text


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
