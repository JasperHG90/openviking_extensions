"""How a URI is reduced to the area of the store it belongs to.

``area`` decides whether an observation counts as connecting things written
apart, so getting it wrong is quiet: too specific and every observation spans
areas trivially, too broad and none ever does.

The parent directory is used rather than a prefix rule, because no fixed depth
survives the shapes real URIs take — a repository under
``resources/github.com/<owner>/<repo>/`` sits five segments deep, an entity
category three.
"""

from __future__ import annotations

import pytest

from .helpers import row


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (
            "viking://user/jasper/memories/entities/a.md",
            "viking://user/jasper/memories/entities",
        ),
        (
            "viking://user/jasper/memories/preferences/jasper/a.md",
            "viking://user/jasper/memories/preferences/jasper",
        ),
        (
            "viking://user/jasper/resources/github.com/acme/repo-a/README.md",
            "viking://user/jasper/resources/github.com/acme/repo-a",
        ),
    ],
)
def test_a_uri_reduces_to_its_directory(uri: str, expected: str) -> None:
    assert row(uri, "text").area == expected


def test_two_files_in_one_directory_are_one_area() -> None:
    """Keeping the filename would make every memory its own area."""
    left = row("viking://user/jasper/memories/entities/a.md", "x")
    right = row("viking://user/jasper/memories/entities/b.md", "y")
    assert left.area == right.area


def test_two_repositories_are_two_areas() -> None:
    """The motivating case: repo A and repo B solving the same problem.

    These sit five segments deep under the host, which is why a fixed
    two-segment prefix collapsed them into one and made the check vacuous.
    """
    left = row("viking://user/jasper/resources/github.com/acme/repo-a/notes.md", "x")
    right = row("viking://user/jasper/resources/github.com/other/repo-b/notes.md", "y")
    assert left.area != right.area


def test_two_handoffs_for_different_projects_are_two_areas() -> None:
    """The real handoff shape, which also nests below a host segment."""
    left = row(
        "viking://user/jasper/resources/handoffs/github.com/j/openviking/h.md", "x"
    )
    right = row("viking://user/jasper/resources/handoffs/github.com/j/memex/h.md", "y")
    assert left.area != right.area


def test_two_entity_categories_are_two_areas() -> None:
    """Deliberate: entity categories are separate folders, so separate areas."""
    left = row("viking://user/jasper/memories/entities/software_project/a.md", "x")
    right = row("viking://user/jasper/memories/entities/dev_tool/b.md", "y")
    assert left.area != right.area
