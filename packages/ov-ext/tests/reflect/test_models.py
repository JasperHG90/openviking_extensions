"""How a URI is reduced to the area of the store it belongs to.

``peer`` decides whether an observation counts as connecting two bodies of
work, so getting it wrong is quiet: too specific and every observation is
trivially cross-peer, too broad and none ever is.
"""

from __future__ import annotations

import pytest

from .helpers import row


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        # The owner prefix is shared by everything, so it distinguishes nothing.
        ("viking://user/jasper/memories/a.md", "memories"),
        ("viking://user/jasper/memories/entities/a.md", "memories/entities"),
        # Deep enough to separate two repositories under resources...
        ("viking://user/jasper/resources/embedder/a.md", "resources/embedder"),
        ("viking://user/jasper/resources/openviking/a.md", "resources/openviking"),
        # ...and shallow enough that a deeper tree stays one area.
        (
            "viking://user/jasper/resources/embedder/deep/nested/a.md",
            "resources/embedder",
        ),
        # Non-user roots keep their own shape.
        ("viking://resources/shared/a.md", "resources/shared"),
        ("viking://a.md", "root"),
    ],
)
def test_a_uri_reduces_to_its_area(uri: str, expected: str) -> None:
    assert row(uri, "text").peer == expected


def test_two_files_in_one_directory_are_one_area() -> None:
    """Keeping the filename would make every memory its own peer."""
    left = row("viking://user/jasper/memories/entities/a.md", "x")
    right = row("viking://user/jasper/memories/entities/b.md", "y")
    assert left.peer == right.peer


def test_two_repositories_are_two_areas() -> None:
    left = row("viking://user/jasper/resources/embedder/a.md", "x")
    right = row("viking://user/jasper/resources/openviking/a.md", "y")
    assert left.peer != right.peer
