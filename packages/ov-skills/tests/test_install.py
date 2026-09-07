"""Checks on ``install.sh``.

The installer writes into directories the user's other tools also own —
``~/.claude/skills`` holds hand-written skills too — so most of what is
asserted here is about what it must *not* disturb.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent
INSTALLER = PACKAGE / "install.sh"
SKILLS = ("handoff", "continue", "learnings", "ingest")


def run(
    *args: str,
    home: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the installer.

    Parameters
    ----------
    *args
        Arguments passed to ``install.sh``.
    home
        Value for ``$HOME``, so harness-default paths land in a sandbox.
    check
        Raise if the installer exits non-zero.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished process.
    """
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def test_installs_every_skill(tmp_path: Path) -> None:
    """A local install lands all four skills, each with its SKILL.md."""
    dest = tmp_path / "skills"
    run("--local", str(PACKAGE), "--to", str(dest), check=True)
    for skill in SKILLS:
        assert (dest / skill / "SKILL.md").is_file()


def test_installs_the_helper_script_executable(tmp_path: Path) -> None:
    """The path helper must arrive with both skills, and be runnable.

    /handoff and /continue agree on where handoffs live only because they run
    this script. An installer that delivered SKILL.md alone would leave both
    skills unable to compute a path, so asserting on SKILL.md is not enough.
    """
    dest = tmp_path / "skills"
    run("--local", str(PACKAGE), "--to", str(dest), check=True)
    for skill in ("handoff", "continue"):
        helper = dest / skill / "scripts" / "ov-handoff-path.sh"
        assert helper.is_file(), f"{skill}: helper was not installed"
        assert helper.stat().st_mode & 0o111, f"{skill}: helper is not executable"


def test_helper_is_made_executable_when_the_payload_is_not(tmp_path: Path) -> None:
    """A payload that arrives without the mode bit still installs runnable.

    ``cp -R`` from a checkout preserves the bit, so this path only shows up
    with a tarball unpacked under a restrictive umask — where the agent would
    otherwise find a helper it cannot execute.
    """
    payload = tmp_path / "payload"
    shutil.copytree(PACKAGE / "skills", payload / "skills")
    for skill in ("handoff", "continue"):
        (payload / "skills" / skill / "scripts" / "ov-handoff-path.sh").chmod(0o644)

    dest = tmp_path / "skills"
    run("--local", str(payload), "--to", str(dest), check=True)

    for skill in ("handoff", "continue"):
        helper = dest / skill / "scripts" / "ov-handoff-path.sh"
        assert helper.stat().st_mode & 0o111, f"{skill}: helper is not executable"


def test_installed_helper_actually_runs(tmp_path: Path) -> None:
    """End to end: the installed copy derives a path in a real repository."""
    dest = tmp_path / "skills"
    run("--local", str(PACKAGE), "--to", str(dest), check=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:acme/api.git",
        ],
        check=True,
    )

    result = subprocess.run(
        ["bash", str(dest / "handoff" / "scripts" / "ov-handoff-path.sh"), "dir"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ("viking://~/resources/handoffs/github.com/acme/api")


def test_accepts_either_the_package_or_its_skills_dir(tmp_path: Path) -> None:
    """``--local`` takes the package root or the skills directory inside it."""
    dest = tmp_path / "skills"
    run("--local", str(PACKAGE / "skills"), "--to", str(dest), check=True)
    assert (dest / "handoff" / "SKILL.md").is_file()


def test_list_writes_nothing(tmp_path: Path) -> None:
    """``--list`` reports the plan without creating the destination."""
    dest = tmp_path / "skills"
    result = run("--local", str(PACKAGE), "--to", str(dest), "--list")
    assert result.returncode == 0
    assert "would install" in result.stderr
    assert not dest.exists()


def test_reinstall_drops_stale_files(tmp_path: Path) -> None:
    """A file left by an older version does not survive the next install.

    Copying over the top would leave it behind, and a stale reference file
    inside a skill directory is read as current.
    """
    dest = tmp_path / "skills"
    run("--local", str(PACKAGE), "--to", str(dest), check=True)
    stale = dest / "handoff" / "OLD_REFERENCE.md"
    stale.write_text("from a previous version", encoding="utf-8")

    run("--local", str(PACKAGE), "--to", str(dest), check=True)
    assert not stale.exists()
    assert (dest / "handoff" / "SKILL.md").is_file()


def test_leaves_unrelated_skills_alone(tmp_path: Path) -> None:
    """Skills the user wrote themselves are not touched.

    The destination is usually a shared directory, so the installer replaces
    only the four names it owns.
    """
    dest = tmp_path / "skills"
    mine = dest / "my-own-skill"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine", encoding="utf-8")

    run("--local", str(PACKAGE), "--to", str(dest), check=True)
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_incomplete_payload_aborts_before_writing(tmp_path: Path) -> None:
    """A payload missing a skill fails without half-installing the rest."""
    src = tmp_path / "src" / "skills" / "handoff"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("partial", encoding="utf-8")
    dest = tmp_path / "dest"

    result = run("--local", str(tmp_path / "src"), "--to", str(dest))
    assert result.returncode == 1
    assert "missing skills/" in result.stderr
    assert not dest.exists()


def test_to_and_target_conflict(tmp_path: Path) -> None:
    """``--to`` names one directory, so it cannot also fan out by harness."""
    result = run("--to", str(tmp_path), "--target", "claude")
    assert result.returncode == 1
    assert "mutually exclusive" in result.stderr


def test_unknown_target_is_rejected() -> None:
    """An unknown harness fails loudly rather than installing nowhere."""
    result = run("--target", "emacs", "--list")
    assert result.returncode == 1
    assert "unknown target" in result.stderr


@pytest.mark.parametrize(
    ("target", "suffix"),
    [
        ("claude", ".claude/skills"),
        ("opencode", ".config/opencode/skills"),
        ("hermes", ".hermes/skills/openviking"),
    ],
)
def test_target_directories(target: str, suffix: str, tmp_path: Path) -> None:
    """Each harness resolves to the directory that harness actually scans."""
    result = run("--target", target, "--list", home=tmp_path)
    assert result.returncode == 0
    assert str(tmp_path / suffix) in result.stderr


def test_hermes_category_is_configurable(tmp_path: Path) -> None:
    """Hermes groups skills in subdirectories, so the group must be settable."""
    result = run("--target", "hermes", "--category", "memory", "--list", home=tmp_path)
    assert str(tmp_path / ".hermes/skills/memory") in result.stderr


def test_detects_installed_harnesses(tmp_path: Path) -> None:
    """With no --target, only the harnesses present on the machine are used."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".hermes").mkdir()

    result = run("--local", str(PACKAGE), "--list", home=tmp_path)
    assert result.returncode == 0
    assert str(tmp_path / ".claude/skills") in result.stderr
    assert str(tmp_path / ".hermes/skills/openviking") in result.stderr
    assert ".config/opencode" not in result.stderr


def test_no_harness_found_is_an_error(tmp_path: Path) -> None:
    """Installing nowhere is a failure, not a silent success."""
    result = run("--local", str(PACKAGE), home=tmp_path)
    assert result.returncode == 1
    assert "found no harness" in result.stderr


def test_installs_to_several_harnesses_at_once(tmp_path: Path) -> None:
    """Repeating --target installs the same skills into each destination."""
    run(
        "--local",
        str(PACKAGE),
        "--target",
        "claude",
        "--target",
        "hermes",
        home=tmp_path,
        check=True,
    )
    assert (tmp_path / ".claude/skills/handoff/SKILL.md").is_file()
    assert (tmp_path / ".hermes/skills/openviking/handoff/SKILL.md").is_file()


def test_home_with_a_space_is_handled(tmp_path: Path) -> None:
    """Paths are joined on newlines precisely so a spaced $HOME still works."""
    home = tmp_path / "My Home"
    (home / ".claude").mkdir(parents=True)

    run("--local", str(PACKAGE), home=home, check=True)
    assert (home / ".claude/skills/handoff/SKILL.md").is_file()


def latest_version_pipeline() -> str:
    """Extract the body of ``latest_version`` from ``install.sh``.

    Returns
    -------
    str
        The function body, with the ``curl`` replaced by a read of ``$1`` so
        the real awk and sort run against a fixture instead of the network.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r"^latest_version\(\) \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "install.sh no longer defines latest_version()"
    body = re.sub(r'curl -fsSL "[^"]*" 2>/dev/null', 'cat "$1"', match.group(1))

    # The function reads TAG_PREFIX from the script's top level. Take it from
    # there too, so the test cannot pass against a prefix the installer no
    # longer uses.
    prefix = re.search(r'^TAG_PREFIX="([^"]+)"', text, re.MULTILINE)
    assert prefix, "install.sh no longer defines TAG_PREFIX"
    return f'TAG_PREFIX="{prefix.group(1)}"\n{body}'


def releases_fixture(entries: list[tuple[str, bool]]) -> str:
    """Render a GitHub releases payload.

    Parameters
    ----------
    entries
        ``(tag_name, prerelease)`` pairs, in the order the API returns them.

    Returns
    -------
    str
        The JSON payload.
    """
    return json.dumps([{"tag_name": tag, "prerelease": pre} for tag, pre in entries])


def test_latest_version_picks_the_highest_not_the_first(tmp_path: Path) -> None:
    """Version order wins over API order.

    The releases endpoint is ordered by creation date, not by version — this
    repository's own releases already come back out of order. Taking the first
    match would hand a bare ``curl | bash`` install an older release.
    """
    payload = tmp_path / "releases.json"
    payload.write_text(
        releases_fixture(
            [
                ("ov-skills-v0.1.0", False),
                ("ov-skills-v0.10.0", False),  # sorts below 0.9.0 as a string
                ("ov-skills-v0.9.0", False),
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", latest_version_pipeline(), "_", str(payload)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "0.10.0"


def test_latest_version_skips_prereleases_and_other_packages(
    tmp_path: Path,
) -> None:
    """A beta is opt-in, and another package's tags are not ours."""
    payload = tmp_path / "releases.json"
    payload.write_text(
        releases_fixture(
            [
                ("ov-skills-v2.0.0", True),  # a beta must not be handed out
                # A sibling package's tag, and deliberately one LONGER than
                # "ov-skills-v". A shorter tag would leave substr() with an
                # empty string that sorts below everything, so the assertion
                # would hold even with the prefix filter removed.
                ("ov-postgres-v9.9.9", False),
                ("ov-skills-v1.2.0", False),
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", latest_version_pipeline(), "_", str(payload)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "1.2.0"


def test_installer_fetches_the_artifact_the_release_builds() -> None:
    """The download name must match what release.yaml actually attaches.

    These live in different files, so a rename in one silently breaks the
    other — and only for real released installs, which no test exercises.

    Skipped when the workflow is absent: the package is also published as a
    standalone plugin directory, and the suite must still run from a copy that
    carries no repository around it.
    """
    release_yaml = PACKAGE.parent.parent / ".github" / "workflows" / "release.yaml"
    if not release_yaml.is_file():
        pytest.skip("release.yaml is outside this package; not a repository checkout")
    workflow = release_yaml.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "dist/ov-skills.tar.gz" in workflow
    assert "/ov-skills.tar.gz" in installer


@pytest.mark.integration
def test_installs_from_main(tmp_path: Path) -> None:
    """The ``--main`` path fetches and unpacks the repo tarball.

    This exercises the tar invocation, which differs between GNU tar and the
    bsdtar macOS ships — the one part of the installer no local test covers.
    """
    dest = tmp_path / "skills"
    env_path = "/usr/bin:/bin:/usr/sbin:/sbin"
    result = subprocess.run(
        ["bash", str(INSTALLER), "--main", "--to", str(dest)],
        capture_output=True,
        text=True,
        env={"PATH": env_path, "HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for skill in SKILLS:
        assert (dest / skill / "SKILL.md").is_file()
