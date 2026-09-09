"""Tests for how a deployment configures the hybrid layer."""

from __future__ import annotations

import pytest

from ov_ext.retrieval.config import ENV_PREFIX, HybridSettings


def test_defaults_have_both_passes_on() -> None:
    """Installing the package should do something without further setup."""
    settings = HybridSettings()

    assert settings.keyword_enabled is True
    assert settings.mmr_enabled is True
    assert settings.mmr_lambda == 0.7


def test_an_environment_variable_overrides_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole configuration surface: no config file, no code change."""
    monkeypatch.setenv(f"{ENV_PREFIX}MMR_LAMBDA", "0.4")
    monkeypatch.setenv(f"{ENV_PREFIX}KEYWORD_ENABLED", "false")

    settings = HybridSettings()

    assert settings.mmr_lambda == 0.4
    assert settings.keyword_enabled is False


def test_an_out_of_range_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside 0..1 the MMR formula stops expressing a trade-off."""
    monkeypatch.setenv(f"{ENV_PREFIX}MMR_LAMBDA", "5.0")

    with pytest.raises(ValueError):
        HybridSettings()


def test_a_misspelled_variable_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tuning knob that silently does nothing is worse than one that fails.

    ``extra="forbid"`` does not cover this: pydantic-settings looks up the
    fields it knows and never enumerates the environment, so a misspelled name
    is not rejected -- it is never read, and the default quietly stands.
    """
    monkeypatch.setenv(f"{ENV_PREFIX}MMR_LAMDA", "0.4")

    with pytest.raises(ValueError, match="Unknown setting"):
        HybridSettings()


def test_the_error_names_the_settings_that_do_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejection has to be actionable, not just a refusal."""
    monkeypatch.setenv(f"{ENV_PREFIX}NONSENSE", "1")

    with pytest.raises(ValueError, match="OV_RETRIEVAL_MMR_LAMBDA"):
        HybridSettings()


def test_an_unrelated_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only this package's own prefix is ours to police."""
    monkeypatch.setenv("OV_POSTGRES_DSN", "postgresql://localhost/x")
    monkeypatch.setenv("PATH_LIKE_THING", "1")

    assert HybridSettings().mmr_lambda == 0.7


def test_the_keyword_clip_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backstop nobody has to switch on. 1024 is memex's number for its own clip."""
    assert HybridSettings().keyword_max_chars == 1024

    monkeypatch.setenv(f"{ENV_PREFIX}KEYWORD_MAX_CHARS", "256")

    assert HybridSettings().keyword_max_chars == 256


def test_a_negative_keyword_clip_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero already means no ceiling, so anything below it is a typo."""
    monkeypatch.setenv(f"{ENV_PREFIX}KEYWORD_MAX_CHARS", "-1")

    with pytest.raises(ValueError):
        HybridSettings()


def test_explicit_arguments_beat_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`install(settings)` has to be able to override a deployment's vars."""
    monkeypatch.setenv(f"{ENV_PREFIX}MMR_LAMBDA", "0.4")

    assert HybridSettings(mmr_lambda=0.9).mmr_lambda == 0.9
