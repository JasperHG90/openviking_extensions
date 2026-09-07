"""Checks on the skill files themselves.

The skills are consumed by three harnesses that each parse the same
``SKILL.md`` frontmatter, so the rules asserted here are the intersection of
what Claude Code, opencode and Hermes accept. A skill that fails one of these
is not "slightly off" — it is invisible to at least one harness.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

PACKAGE = Path(__file__).resolve().parent.parent
SKILLS_DIR = PACKAGE / "skills"
INSTALLER = PACKAGE / "install.sh"

# opencode validates the name against this and refuses anything else
# (packages/opencode/src/skill/index.ts). Claude Code and Hermes are looser, so
# this is the binding constraint.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024


def skill_dirs() -> list[Path]:
    """Return every skill directory in the package, sorted by name."""
    return sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())


def frontmatter(skill: Path) -> dict[str, object]:
    """Parse the YAML frontmatter of a skill's ``SKILL.md``.

    Parameters
    ----------
    skill
        The skill directory.

    Returns
    -------
    dict[str, object]
        The parsed frontmatter mapping.
    """
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"{skill.name}: SKILL.md has no frontmatter block")
    _, _, rest = text.partition("---\n")
    block, sep, _ = rest.partition("\n---\n")
    if not sep:
        raise AssertionError(f"{skill.name}: frontmatter block is not closed")
    parsed = yaml.safe_load(block)
    if not isinstance(parsed, dict):
        raise AssertionError(f"{skill.name}: frontmatter is not a mapping")
    return parsed


def installer_skill_list() -> list[str]:
    """Return the skill names ``install.sh`` declares it ships."""
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r'^SKILLS="([^"]+)"', text, re.MULTILINE)
    assert match, 'install.sh no longer defines SKILLS="..."'
    return match.group(1).split()


@pytest.mark.parametrize("skill", skill_dirs(), ids=lambda p: p.name)
def test_skill_has_valid_frontmatter(skill: Path) -> None:
    """Every skill parses, and its name and description satisfy all harnesses."""
    data = frontmatter(skill)

    name = data.get("name")
    assert isinstance(name, str), f"{skill.name}: name must be a string"
    # opencode requires the name to match the directory; a mismatch loads the
    # skill under a name no one can invoke.
    assert name == skill.name, f"{skill.name}: name is {name!r}"
    assert NAME_PATTERN.match(name), f"{name!r} is not lowercase-kebab"
    assert len(name) <= MAX_NAME_LENGTH

    description = data.get("description")
    assert isinstance(description, str), f"{skill.name}: description must be a string"
    assert description.strip(), f"{skill.name}: description is empty"
    assert len(description) <= MAX_DESCRIPTION_LENGTH, (
        f"{skill.name}: description is {len(description)} chars, "
        f"over opencode's {MAX_DESCRIPTION_LENGTH} limit"
    )


@pytest.mark.parametrize("skill", skill_dirs(), ids=lambda p: p.name)
def test_skill_body_is_not_empty(skill: Path) -> None:
    """A skill whose body is only frontmatter would load and then say nothing."""
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    _, _, rest = text.partition("---\n")
    _, _, body = rest.partition("\n---\n")
    assert len(body.strip()) > 200, f"{skill.name}: body is suspiciously short"


def test_installer_ships_exactly_the_skills_present() -> None:
    """The installer's list and the skills on disk must not drift apart.

    A skill added to ``skills/`` but missing from ``SKILLS`` is silently never
    installed; a name in ``SKILLS`` with no directory aborts every install.
    """
    assert sorted(installer_skill_list()) == sorted(p.name for p in skill_dirs())


def test_plugin_manifest_matches_the_package() -> None:
    """The Claude Code manifest is valid JSON and points at the real skills."""
    manifest = json.loads(
        (PACKAGE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "ov-skills"
    assert manifest["skills"] == "./skills/"
    assert (PACKAGE / "skills").is_dir()
    # The version is stamped at release time from the git tag, exactly as ovx's
    # is. A real number committed here would be a second source of truth.
    assert manifest["version"] == "0.0.0"


# --- the project-path derivation, shared by /handoff and /continue -----------
#
# These two skills must compute the same path. If /handoff writes somewhere
# /continue does not look, resume silently finds nothing — a failure invisible
# until someone actually needs their handoff back. That is why the derivation
# is a script both skills ship rather than an instruction each follows.

SCRIPT_NAME = "scripts/ov-handoff-path.sh"
SCRIPT = SKILLS_DIR / "handoff" / SCRIPT_NAME


def test_both_skills_ship_the_same_script() -> None:
    """The two copies must stay byte-identical, or the paths diverge."""
    handoff = (SKILLS_DIR / "handoff" / SCRIPT_NAME).read_bytes()
    resume = (SKILLS_DIR / "continue" / SCRIPT_NAME).read_bytes()
    assert handoff == resume


@pytest.mark.parametrize("skill", ["handoff", "continue"])
def test_script_is_executable(skill: str) -> None:
    """A copied-in script that lost its mode bit cannot be run by the agent."""
    path = SKILLS_DIR / skill / SCRIPT_NAME
    assert path.stat().st_mode & 0o111, f"{skill}: {SCRIPT_NAME} is not executable"


@pytest.mark.parametrize("skill", ["handoff", "continue"])
def test_skill_tells_the_agent_to_run_the_script(skill: str) -> None:
    """Each skill must point at the script rather than describe the derivation.

    A skill that spells the rule out in prose invites the model to reproduce it
    from memory, which is the drift this script exists to prevent.
    """
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    assert SCRIPT_NAME in text
    assert "git remote get-url" not in text


def git_repo(tmp_path: Path, remote: str) -> Path:
    """Create a git repository whose ``origin`` is *remote*.

    Parameters
    ----------
    tmp_path
        Directory to create the repository in.
    remote
        The URL to set as ``origin``.

    Returns
    -------
    Path
        The repository's working directory.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", remote], check=True
    )
    return tmp_path


def run_script(cwd: Path, *args: str) -> str:
    """Run the path script in *cwd* and return its trimmed stdout."""
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        # Every spelling of one repo must fold onto one path, or handoffs
        # scatter across directories depending on how the user cloned.
        (
            "https://github.com/JasperHG90/openviking_extensions.git",
            "github.com/jasperhg90/openviking_extensions",
        ),
        (
            "https://github.com/JasperHG90/openviking_extensions",
            "github.com/jasperhg90/openviking_extensions",
        ),
        (
            "git@github.com:JasperHG90/openviking_extensions.git",
            "github.com/jasperhg90/openviking_extensions",
        ),
        (
            "ssh://git@github.com/JasperHG90/openviking_extensions.git",
            "github.com/jasperhg90/openviking_extensions",
        ),
        # Case folding, so two spellings differing only in case converge.
        ("https://GitHub.com/Acme/API.git", "github.com/acme/api"),
        # A self-hosted forge with a deeper path keeps its nesting.
        (
            "https://git.example.com/team/group/repo.git",
            "git.example.com/team/group/repo",
        ),
        # A non-default port is not a path component. Left in, moving the
        # forge to another port would orphan every existing handoff.
        (
            "https://git.example.com:8443/team/repo.git",
            "git.example.com/team/repo",
        ),
        (
            "ssh://git@git.example.com:2222/team/repo.git",
            "git.example.com/team/repo",
        ),
        # A trailing slash after .git is the same repo, not a second one.
        ("https://github.com/acme/api.git/", "github.com/acme/api"),
        # In scp form there is no scheme, so digits after the colon start the
        # path -- they are not a port to strip.
        ("git@github.com:2222/repo.git", "github.com/2222/repo"),
        # A trailing slash is not a new directory level.
        ("https://github.com/acme/api/", "github.com/acme/api"),
        # A token in the remote must not reach the path.
        ("https://oauth2:ghp_secret@github.com/acme/api.git", "github.com/acme/api"),
    ],
)
def test_project_path_from_remote(remote: str, expected: str, tmp_path: Path) -> None:
    """Each remote spelling resolves to the same readable project path."""
    repo = git_repo(tmp_path, remote)
    assert run_script(repo, "project") == expected


@pytest.mark.parametrize(
    "remote",
    [
        # A filesystem remote is one machine's directory, not a shared
        # identity. Left alone it yields a leading slash, so the URI has an
        # empty first segment.
        "/srv/git/repo.git",
        # A relative clone. Passed through, this reaches the server as
        # `handoffs/../sibling`, which its traversal guard rejects outright --
        # so both skills would hard-fail rather than fall back.
        "../sibling",
        # A remote with a host and no path gives nothing to scope by.
        "https://github.com",
    ],
)
def test_unusable_remotes_fall_back(remote: str, tmp_path: Path) -> None:
    """A remote that is not a shared identity falls back to the repo name."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    git_repo(repo, remote)
    assert run_script(repo, "project") == "local/myproject"


def test_project_path_outside_a_repo(tmp_path: Path) -> None:
    """A plain directory is scoped by its name, under ``local/``."""
    scratch = tmp_path / "Some Scratch Dir"
    scratch.mkdir()
    assert run_script(scratch, "project") == "local/some-scratch-dir"


@pytest.mark.parametrize("remote", [None, "git@github.com:acme/api.git"])
def test_same_answer_from_a_subdirectory(remote: str | None, tmp_path: Path) -> None:
    """Where you stand in the repo must not change where handoffs go.

    /handoff run from the root and /continue run from a subdirectory have to
    agree. Scoping the fallback by ``$PWD`` broke exactly this, and it breaks
    silently: resume just finds nothing.
    """
    repo = tmp_path / "myproject"
    (repo / "src" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if remote is not None:
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", remote], check=True
        )

    assert run_script(repo, "project") == run_script(repo / "src" / "deep", "project")


def test_dir_uri(tmp_path: Path) -> None:
    """``dir`` is the handoff folder for the project, as a viking:// URI."""
    repo = git_repo(tmp_path, "git@github.com:acme/api.git")
    assert run_script(repo, "dir") == "viking://~/resources/handoffs/github.com/acme/api"


def test_new_uri_shape(tmp_path: Path) -> None:
    """``new`` stamps a sortable UTC time and slugifies the description."""
    repo = git_repo(tmp_path, "git@github.com:acme/api.git")
    uri = run_script(repo, "new", "Vault Routing!! (part 2)")
    assert re.fullmatch(
        r"viking://~/resources/handoffs/github\.com/acme/api"
        r"/\d{4}-\d{2}-\d{2}T\d{4}--vault-routing-part-2\.md",
        uri,
    ), uri


def test_new_uri_survives_an_unusable_slug(tmp_path: Path) -> None:
    """A slug of pure punctuation must not produce a filename ending in ``--``."""
    repo = git_repo(tmp_path, "git@github.com:acme/api.git")
    uri = run_script(repo, "new", "!!!")
    assert uri.endswith("--handoff.md"), uri


def test_unknown_command_fails(tmp_path: Path) -> None:
    """An unrecognised command exits non-zero rather than printing a bad path."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "bogus"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Usage:" in result.stderr
