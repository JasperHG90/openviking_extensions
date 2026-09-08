"""Fixtures for the reflection tests.

The stand-in store and model live in ``helpers.py`` -- they are builders,
not fixtures, and a test that wants one constructs it with the rows that
test is about.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every OV_REFLECT_ variable so settings read their defaults.

    The misspelling guard enumerates the real environment, so a stray variable
    on the developer's machine would fail unrelated tests.
    """
    for name in list(os.environ):
        if name.upper().startswith("OV_REFLECT_"):
            monkeypatch.delenv(name, raising=False)
