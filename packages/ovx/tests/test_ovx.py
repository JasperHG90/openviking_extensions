"""Tests for ovx.sh.

The suite drives the real script. A shim on PATH stands in for ``ov`` and
records what it saw — its argv, ``$OPENVIKING_CLI_CONFIG_FILE``, and the
contents and permissions of the file that variable pointed at — so the tests
can assert on the config ovx materialized without ovx having to expose it.

Interactive paths (the wizard, the menus) read from ``/dev/tty``, so those
tests drive the script through a pty.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

OVX = Path(__file__).resolve().parent.parent / "ovx.sh"

# Resolved at import, while PATH and HOME are still the real ones. The
# workspace fixture redirects both, so a later lookup would miss.
REAL_OV = shutil.which("ov")

# The shim records what ovx handed it, then exits with $OVX_TEST_EXIT so a
# test can check that ov's exit code is propagated.
#
# `stat` spells "permission bits" differently on GNU and BSD, and the GNU
# probe has to come first: BSD stat has no -c and fails, so the fallback
# fires, but GNU's -f means "filesystem status" and SUCCEEDS with unrelated
# output, so trying BSD first never falls back and yields ' File: "..."'.
SHIM = """#!/usr/bin/env bash
mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
{
  echo "ARGS: $*"
  echo "CONF: ${OPENVIKING_CLI_CONFIG_FILE:-<unset>}"
  if [ -f "${OPENVIKING_CLI_CONFIG_FILE:-/nonexistent}" ]; then
    echo "FILEMODE: $(mode "$OPENVIKING_CLI_CONFIG_FILE")"
    echo "DIRMODE: $(mode "$(dirname "$OPENVIKING_CLI_CONFIG_FILE")")"
    echo "BODY_START"
    cat "$OPENVIKING_CLI_CONFIG_FILE"
    echo "BODY_END"
  fi
} >> "$OVX_TEST_LOG"
sleep "${OVX_TEST_SLEEP:-0}"
exit "${OVX_TEST_EXIT:-0}"
"""


class Shim:
    """What the fake ``ov`` recorded during one ovx run.

    Attributes
    ----------
    args : str
        The argv ovx passed to ov, space-joined.
    conf_path : str
        Value of ``$OPENVIKING_CLI_CONFIG_FILE`` as ov saw it.
    body : str
        Contents of the config file at that path, empty if there was none.
    file_mode : str
        Octal permissions of the config file, e.g. ``600``.
    dir_mode : str
        Octal permissions of its directory, e.g. ``700``.
    """

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.args = self._field("ARGS")
        self.conf_path = self._field("CONF")
        self.file_mode = self._field("FILEMODE")
        self.dir_mode = self._field("DIRMODE")
        match = re.search(r"BODY_START\n(.*)BODY_END", raw, re.DOTALL)
        self.body = match.group(1) if match else ""

    def _field(self, name: str) -> str:
        match = re.search(rf"^{name}: (.*)$", self.raw, re.MULTILINE)
        return match.group(1) if match else ""

    @property
    def ran(self) -> bool:
        """Whether ov was invoked at all."""
        return bool(self.raw.strip())

    @property
    def config(self) -> dict[str, Any]:
        """The materialized ovcli.conf, parsed."""
        return json.loads(self.body)  # type: ignore[no-any-return]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give the test a private HOME, config file, and PATH with the ov shim.

    Everything ovx touches — ``$HOME``, ``~/.ovx``, the ``ov`` binary — is
    redirected under ``tmp_path`` so no test can read or write the real
    OpenViking config.
    """
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "ov"
    shim.write_text(SHIM)
    shim.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OVX_DIR", str(tmp_path / "ovx"))
    monkeypatch.setenv("OVX_CONFIG_FILE", str(tmp_path / "config.toml"))
    monkeypatch.setenv("OVX_TEST_LOG", str(tmp_path / "shim.log"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path


@pytest.fixture
def config(workspace: Path) -> Path:
    """Path to the ovx config file for this test."""
    return workspace / "config.toml"


def write_config(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as the ovx profile config."""
    path.write_text(text)


def run(args: list[str], **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run ovx headless with the current environment plus ``overrides``."""
    env = dict(os.environ)
    env.update(overrides)
    return subprocess.run(
        [str(OVX), *args],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def shim_log(workspace: Path) -> Shim:
    """Read what the ov shim recorded, if it ran."""
    log = workspace / "shim.log"
    return Shim(log.read_text() if log.exists() else "")


def run_pty(args: list[str], steps: list[tuple[str, str]], timeout: int = 20) -> str:
    """Run ovx under a pty, answering each prompt as it appears.

    ``steps`` is a list of ``(needle, text)`` pairs. ``text`` is written once
    ``needle`` first appears in the cumulative output, in order. Gating on the
    specific prompt rather than on "a chunk arrived" keeps the driver stable
    when several prompts coalesce into one read.

    Parameters
    ----------
    args :
        Arguments to pass to ovx.
    steps :
        Prompt/response pairs, applied in order.
    timeout :
        Seconds to wait before giving up on the child.

    Returns
    -------
    str
        Everything the child wrote to the pty.
    """
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - runs in the forked child
        os.execvpe(str(OVX), [str(OVX), *args], dict(os.environ))
        os._exit(127)
    buf = b""
    i = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        while i < len(steps) and steps[i][0].encode() in buf:
            os.write(fd, steps[i][1].encode())
            i += 1
            time.sleep(0.05)
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                # Drain what is still buffered: the last messages are written
                # just before exit and would otherwise be lost.
                while True:
                    ready, _, _ = select.select([fd], [], [], 0.1)
                    if not ready:
                        break
                    try:
                        tail = os.read(fd, 4096)
                    except OSError:
                        tail = b""
                    if not tail:
                        break
                    buf += tail
                break
        except ChildProcessError:
            break
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    os.close(fd)
    return buf.decode(errors="replace")


LAB = '["lab"]\nurl = "https://ov.example.com"\napi_key = "$OV_KEY"\n'


# --- materializing the config -------------------------------------------


def test_expands_var_and_hands_config_to_ov(workspace: Path, config: Path) -> None:
    """The key comes from the environment and reaches ov through a temp file."""
    write_config(config, LAB)
    result = run(["lab", "find", "a query"], OV_KEY="secret-from-env")
    assert result.returncode == 0, result.stderr
    shim = shim_log(workspace)
    assert shim.ran, "ov was never invoked"
    assert shim.args == "find a query"
    assert shim.config == {
        "url": "https://ov.example.com",
        "api_key": "secret-from-env",
    }


def test_temp_config_is_removed_after_ov_exits(workspace: Path, config: Path) -> None:
    """The materialized ovcli.conf does not outlive the command."""
    write_config(config, LAB)
    run(["lab", "status"], OV_KEY="k")
    shim = shim_log(workspace)
    assert shim.conf_path.startswith("/"), f"ov got no config path: {shim.conf_path}"
    assert not Path(shim.conf_path).exists(), f"temp config survived at {shim.conf_path}"
    assert not Path(shim.conf_path).parent.exists(), "temp directory survived"


def test_temp_config_is_removed_when_ov_fails(workspace: Path, config: Path) -> None:
    """A failing ov still gets its config cleaned up, and its code propagated."""
    write_config(config, LAB)
    result = run(["lab", "status"], OV_KEY="k", OVX_TEST_EXIT="7")
    assert result.returncode == 7, result.stderr
    shim = shim_log(workspace)
    # Guard before the exists() check: an unexported OPENVIKING_CLI_CONFIG_FILE
    # logs "<unset>", and Path("<unset>").exists() is False, so without this
    # the assertion below would pass for exactly the bug it is meant to catch.
    assert shim.conf_path.startswith("/"), f"ov got no config path: {shim.conf_path}"
    assert not Path(shim.conf_path).exists()


def test_temp_config_is_removed_on_sigint(workspace: Path, config: Path) -> None:
    """Ctrl-C mid-command cleans up too — the trap, not the exit path, does it."""
    write_config(config, LAB)
    env = dict(os.environ)
    env.update(OV_KEY="k", OVX_TEST_SLEEP="10")
    # Its own session, so the signal goes to the whole group the way a real
    # Ctrl-C at a terminal would, reaching both ovx and the ov it is running.
    proc = subprocess.Popen(
        [str(OVX), "lab", "status"],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    log = workspace / "shim.log"
    deadline = time.time() + 15
    while time.time() < deadline:
        if log.exists() and "CONF:" in log.read_text():
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a hang
        proc.kill()
        pytest.fail("ov never started")

    conf_path = Path(shim_log(workspace).conf_path)
    assert conf_path.exists(), "the config should still be there while ov runs"

    os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    proc.communicate(timeout=15)

    assert not conf_path.exists(), f"config survived a SIGINT at {conf_path}"
    assert not conf_path.parent.exists(), "temp directory survived a SIGINT"
    # ovx traps SIGINT, cleans up, then re-raises it, so the interrupt stays
    # visible to the caller no matter what ov's own exit status was. Without
    # the trap this varied between -2, 130 and even 0 depending on scheduling.
    assert proc.returncode == -signal.SIGINT, proc.returncode


def test_temp_config_permissions_are_private(workspace: Path, config: Path) -> None:
    """The config is 600 inside a 700 directory while ov reads it."""
    write_config(config, LAB)
    run(["lab", "status"], OV_KEY="k")
    shim = shim_log(workspace)
    assert shim.file_mode == "600", f"config mode was {shim.file_mode}"
    assert shim.dir_mode == "700", f"temp dir mode was {shim.dir_mode}"


def test_real_openviking_dir_is_never_written(workspace: Path, config: Path) -> None:
    """Ovx leaves ~/.openviking alone; that is the point of the tool."""
    write_config(config, LAB)
    run(["lab", "status"], OV_KEY="k")
    assert not (workspace / "home" / ".openviking").exists()


def test_unset_var_is_a_hard_error(workspace: Path, config: Path) -> None:
    """An unresolved $VAR fails loudly instead of running against a bad config."""
    write_config(config, LAB)
    env = dict(os.environ)
    env.pop("OV_KEY", None)
    result = subprocess.run(
        [str(OVX), "lab", "status"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 4, result.stderr
    assert "OV_KEY" in result.stderr
    assert not shim_log(workspace).ran, "ov ran against an unresolved config"


def test_empty_var_is_treated_as_unset(workspace: Path, config: Path) -> None:
    """An empty $VAR is an error too — an empty key is never what was meant."""
    write_config(config, LAB)
    result = run(["lab", "status"], OV_KEY="")
    assert result.returncode == 4, result.stderr
    assert not shim_log(workspace).ran


def test_braced_var_and_nested_table_expand(workspace: Path, config: Path) -> None:
    """${VAR} works, and expansion reaches inside a nested table."""
    write_config(
        config,
        '["lab"]\n'
        'url = "https://ov.example.com"\n'
        'api_key = "${OV_KEY}"\n'
        'extra_headers = { X-Trace = "$OV_TRACE" }\n',
    )
    result = run(["lab", "status"], OV_KEY="k1", OV_TRACE="t1")
    assert result.returncode == 0, result.stderr
    assert shim_log(workspace).config["extra_headers"] == {"X-Trace": "t1"}


def test_literal_key_still_works(workspace: Path, config: Path) -> None:
    """A literal value is passed through unchanged."""
    write_config(config, '["lab"]\nurl = "https://x"\napi_key = "sk-literal"\n')
    run(["lab", "status"])
    assert shim_log(workspace).config["api_key"] == "sk-literal"


def test_profile_without_api_key_is_allowed(workspace: Path, config: Path) -> None:
    """Ov treats api_key as optional, so ovx must not invent a requirement."""
    write_config(config, '["local"]\nurl = "http://127.0.0.1:1933"\n')
    result = run(["local", "status"])
    assert result.returncode == 0, result.stderr
    assert shim_log(workspace).config == {"url": "http://127.0.0.1:1933"}


def test_non_string_fields_keep_their_json_type(workspace: Path, config: Path) -> None:
    """A number stays a number and a bool stays a bool in the written JSON."""
    write_config(
        config,
        '["lab"]\nurl = "https://x"\ntimeout = 30.5\nverbose = true\n',
    )
    run(["lab", "status"])
    config_written = shim_log(workspace).config
    assert config_written["timeout"] == 30.5
    assert config_written["verbose"] is True


# --- rejecting bad profiles ---------------------------------------------


def test_unknown_field_is_rejected(workspace: Path, config: Path) -> None:
    """A typo'd key fails here rather than being silently ignored by ov."""
    write_config(config, '["lab"]\nurl = "https://x"\napi_kye = "oops"\n')
    result = run(["lab", "status"])
    assert result.returncode == 2, result.stderr
    assert "api_kye" in result.stderr
    assert not shim_log(workspace).ran


def test_missing_url_is_rejected(workspace: Path, config: Path) -> None:
    """Url is the one field a profile cannot omit."""
    write_config(config, '["lab"]\napi_key = "k"\n')
    result = run(["lab", "status"])
    assert result.returncode == 2, result.stderr
    assert "url" in result.stderr
    assert not shim_log(workspace).ran


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ('url = "https://x"\ntimeout = "soon"\n', "timeout"),
        ('url = "https://x"\nverbose = "yes"\n', "verbose"),
        ('url = "https://x"\ntimeout = true\n', "timeout"),
        ("url = 3\n", "url"),
    ],
)
def test_wrong_type_is_rejected(
    workspace: Path, config: Path, body: str, needle: str
) -> None:
    """A value of the wrong type fails before ov sees the file."""
    write_config(config, f'["lab"]\n{body}')
    result = run(["lab", "status"])
    assert result.returncode == 2, result.stderr
    assert needle in result.stderr
    assert not shim_log(workspace).ran


def test_unreadable_config_reports_cleanly(workspace: Path, config: Path) -> None:
    """A config that cannot be read gives a message, not a Python traceback."""
    write_config(config, LAB)
    config.chmod(0o000)
    try:
        result = run(["lab", "status"], OV_KEY="k")
    finally:
        config.chmod(0o600)  # so tmp_path cleanup can remove it
    assert result.returncode == 1, result.stderr
    assert "cannot read" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert not shim_log(workspace).ran


def test_unknown_profile_is_rejected(workspace: Path, config: Path) -> None:
    """Naming a profile that does not exist lists what does."""
    write_config(config, LAB)
    result = run(["nope", "status"])
    assert result.returncode == 1
    assert "not found" in result.stderr
    assert "lab" in result.stderr


# --- argument forwarding -------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["lab"], ""),
        (["lab", "status"], "status"),
        (["lab", "find", "two words"], "find two words"),
        (["lab", "--", "-o", "json"], "-o json"),
        (["lab", "-o", "json", "status"], "-o json status"),
    ],
)
def test_args_reach_ov_untouched(
    workspace: Path, config: Path, argv: list[str], expected: str
) -> None:
    """Everything after the profile name is forwarded to ov verbatim."""
    write_config(config, LAB)
    result = run(argv, OV_KEY="k")
    assert result.returncode == 0, result.stderr
    assert shim_log(workspace).args == expected


def test_unknown_ovx_option_is_refused(workspace: Path, config: Path) -> None:
    """An option before the profile belongs to ovx, and an unknown one errors."""
    write_config(config, LAB)
    result = run(["--bogus"])
    assert result.returncode == 1
    assert "unknown option" in result.stderr
    assert not shim_log(workspace).ran


def test_help_and_version_need_nothing_installed(workspace: Path) -> None:
    """-h and -V answer before any dependency check, so they always work."""
    # bash for the shebang and awk for the help text, which is all -h and -V
    # may rely on. Deliberately no python3 and no ov.
    minimal = workspace / "nobin"
    minimal.mkdir()
    for tool in ("bash", "awk"):
        found = shutil.which(tool)
        assert found is not None, tool
        (minimal / tool).symlink_to(found)
    env = dict(os.environ, PATH=str(minimal))

    for flag in ("-h", "--help"):
        result = subprocess.run(
            [str(OVX), flag], env=env, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert "Usage: ovx [PROFILE]" in result.stdout, result.stdout

    result = subprocess.run(
        [str(OVX), "-V"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    # "dev" in a checkout; release.yaml stamps the tag's version in its place.
    assert re.fullmatch(r"ovx \S+\n", result.stdout), result.stdout


def test_help_reports_the_config_file_in_force(workspace: Path, config: Path) -> None:
    """The header documents the default path; --help names the one in use."""
    result = run(["--help"])
    assert result.returncode == 0, result.stderr
    assert f"Config in use: {config}" in result.stdout, result.stdout


def test_every_option_is_documented(workspace: Path) -> None:
    """Each flag the parser accepts appears in --help.

    The help text is the comment block at the top of ovx.sh, so it is edited
    in a different place from the case statement that implements the flags.
    This is what stops the two drifting apart.
    """
    source = OVX.read_text()
    case_body = source.split("--- arg parsing ---")[1]
    # Long options, with their short alias where there is one. A long-only arm
    # has to be caught too, which an alias-only pattern would silently miss.
    # The bare "--)" separator and the "-*)" catch-all are not options and do
    # not match.
    arms = re.findall(r"^\s+((?:-\w\|)?--[\w-]+)\)", case_body, re.MULTILINE)
    assert arms, "found no option cases to check"

    help_text = run(["--help"]).stdout
    for arm in sorted(set(arms)):
        expected = arm.replace("|", ", ")
        assert expected in help_text, f"{arm} is undocumented"


def test_list_does_not_need_ov(workspace: Path, config: Path) -> None:
    """--list works without ov installed and prints each profile's url."""
    write_config(config, LAB)
    # A PATH holding a working python3 and deliberately no ov. Trimming to
    # /usr/bin would drop ov but also leave macOS's python3, which is 3.9 and
    # has no tomllib — that would fail for the wrong reason.
    minimal = workspace / "nobin"
    minimal.mkdir()
    (minimal / "python3").symlink_to(sys.executable)
    bash = shutil.which("bash")
    assert bash is not None
    (minimal / "bash").symlink_to(bash)  # the shebang resolves through PATH
    env = dict(os.environ)
    env["PATH"] = str(minimal)
    assert shutil.which("ov", path=env["PATH"]) is None
    result = subprocess.run(
        [str(OVX), "-l"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "lab" in result.stdout
    assert "https://ov.example.com" in result.stdout


# --- interactive paths ---------------------------------------------------


def test_no_tty_gate(workspace: Path, config: Path) -> None:
    """An interactive action without a terminal refuses cleanly, not a crash."""
    write_config(config, LAB)
    result = run(["-e", "lab"])
    assert result.returncode == 1, result.stderr
    assert "interactive terminal" in result.stderr


def test_create_then_run(
    workspace: Path, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard writes a profile and immediately runs ov with it."""
    # run_pty passes the live environment to the child, so the var has to be
    # set here. monkeypatch, not os.environ: a developer who exports OV_KEY
    # must get it back when the test ends.
    monkeypatch.setenv("OV_KEY", "from-env")
    out = run_pty(
        ["-n", "status"],
        [
            ("Profile name", "lab\n"),
            ("url [", "https://new.example.com\n"),
            ("api_key", "$OV_KEY\n"),
            ("account [", "acme\n"),
            ("user [", "jasper\n"),
        ],
    )
    assert "not found" not in out, f"the launch saw a corrupted name:\n{out}"
    saved = tomllib.loads(config.read_text())
    assert saved["lab"]["api_key"] == "$OV_KEY", "the wizard expanded the var too early"
    assert saved["lab"]["account"] == "acme"
    shim = shim_log(workspace)
    assert shim.ran, f"ov never ran:\n{out}"
    assert shim.config["api_key"] == "from-env", "the var was not expanded at launch"


def test_config_file_is_private(workspace: Path, config: Path) -> None:
    """The profile file the wizard writes is 600, since it may hold a literal."""
    out = run_pty(
        ["-n"],
        [
            ("Profile name", "lab\n"),
            ("url [", "https://x\n"),
            ("api_key", "sk-literal\n"),
            ("account [", "\n"),
            ("user [", "\n"),
        ],
    )
    assert config.exists(), out
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_edit_preserves_comments_and_hand_added_fields(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit rewrites what it prompts for and leaves everything else alone."""
    write_config(
        config,
        "# keep me\n"
        '["lab"]\n'
        "# url note\n"
        'url = "https://old.example.com"\n'
        'api_key = "$OV_KEY"\n'
        "timeout = 30.0\n"  # not a wizard field
        "\n"
        '["other"]\n'
        'url = "https://other.example.com"\n',
    )
    monkeypatch.setenv("OV_KEY", "k")
    run_pty(
        ["-e", "lab"],
        [
            ("url [", "https://new.example.com\n"),
            ("api_key", "\n"),  # keep current
            ("account [", "\n"),
            ("user [", "\n"),
        ],
    )
    after = config.read_text()
    assert "# keep me" in after, after
    assert "# url note" in after, after
    parsed = tomllib.loads(after)
    assert parsed["lab"]["url"] == "https://new.example.com"
    assert parsed["lab"]["api_key"] == "$OV_KEY", "the kept key was expanded"
    assert parsed["lab"]["timeout"] == 30.0, "a hand-added field was dropped"
    assert parsed["other"]["url"] == "https://other.example.com"


def test_delete_confirmed_removes_only_target(config: Path) -> None:
    """A confirmed delete removes one section and leaves the rest intact."""
    write_config(
        config,
        '["lab"]\nurl = "https://a"\n\n["other"]\nurl = "https://b"\n',
    )
    out = run_pty(["-d", "lab"], [("[y/N]", "y\n")])
    assert "deleted profile 'lab'" in out, out
    parsed = tomllib.loads(config.read_text())
    assert set(parsed) == {"other"}


# A dotted header declares the same top-level name to tomllib, which lists the
# profile, but no literal [prod] line exists for the section rewriter to find.
# It must refuse rather than report a success it did not perform.
DOTTED = '["prod".eu]\nurl = "https://a"\napi_key = "$OV_KEY"\n'


def test_delete_refuses_a_profile_it_cannot_locate(config: Path) -> None:
    """A dotted-table profile is refused, not reported as deleted."""
    write_config(config, DOTTED)
    before = config.read_text()
    out = run_pty(["-d", "prod"], [("[y/N]", "y\n")])
    assert "not a plain [prod] section" in out, out
    assert "deleted profile" not in out, out
    assert config.read_text() == before, "the config was modified anyway"


def test_edit_refuses_a_profile_it_cannot_locate(config: Path) -> None:
    """Likewise for an edit: no silent 'updated' after a failed rewrite."""
    write_config(config, DOTTED)
    before = config.read_text()
    out = run_pty(
        ["-e", "prod"],
        [
            ("url [", "https://new.example.com\n"),
            ("api_key", "\n"),
            ("account [", "\n"),
            ("user [", "\n"),
        ],
    )
    assert "not a plain [prod] section" in out, out
    assert "updated profile" not in out, out
    assert config.read_text() == before, "the config was modified anyway"


def test_delete_declined_keeps_profile(config: Path) -> None:
    """Anything but y/yes leaves the config untouched."""
    write_config(config, '["lab"]\nurl = "https://a"\n')
    before = config.read_text()
    out = run_pty(["-d", "lab"], [("[y/N]", "\n")])
    assert "cancelled" in out, out
    assert config.read_text() == before


def test_menu_picks_a_profile_and_runs(workspace: Path, config: Path) -> None:
    """Bare ovx offers a menu, and the pick is what ov runs against."""
    write_config(
        config,
        '["lab"]\nurl = "https://a"\n\n["other"]\nurl = "https://b"\n',
    )
    out = run_pty(["--", "status"], [("Choose: ", "2\n")])
    shim = shim_log(workspace)
    assert shim.ran, f"ov never ran:\n{out}"
    assert shim.config["url"] == "https://b"
    assert shim.args == "status"


# --- against the real ov -------------------------------------------------


@pytest.fixture
def echo_server() -> Iterator[tuple[str, list[dict[str, str]]]]:
    """Serve a fake OpenViking /health and collect the headers it is sent."""
    import http.server
    import threading

    seen: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            """Stay quiet; the test asserts on captured headers, not stderr."""

        def do_GET(self) -> None:
            """Record the request headers and answer with a healthy status."""
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = b'{"status":"ok","healthy":true,"components":{}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_real_ov_reads_the_materialized_config(
    workspace: Path,
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    echo_server: tuple[str, list[dict[str, str]]],
) -> None:
    """The real ov binary honors the temp config and sends the expanded key.

    The shim proves ovx writes what it promises; only the real binary proves
    ov reads it. This is the contract the whole design rests on.
    """
    url, seen = echo_server
    if REAL_OV is None:
        pytest.skip("the ov CLI is not installed")
    # The real ov, and a python3 new enough for ovx. Not the shim directory:
    # this test is about the actual binary.
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                str(Path(REAL_OV).parent),
                str(Path(sys.executable).parent),
                "/usr/bin",
                "/bin",
            ]
        ),
    )
    ov_home = workspace / "home" / ".openviking"
    ov_home.mkdir(parents=True)
    # ov resolves its language from ~/.openviking/ovcli.settings.conf, which
    # OPENVIKING_CLI_CONFIG_FILE does not redirect, and refuses to run any
    # command until one exists. The fixture's HOME is empty, so seed it.
    (ov_home / "ovcli.settings.conf").write_text('{"language":"en"}')
    # Every real user has this file — it is the one ovx exists to stop holding
    # a key. Seeding a decoy proves the temp config wins, rather than the tool
    # only appearing to work because nothing competed with it.
    (ov_home / "ovcli.conf").write_text(
        json.dumps({"url": "http://127.0.0.1:9", "api_key": "key-from-home"})
    )

    write_config(config, f'["lab"]\nurl = "{url}"\napi_key = "$OV_KEY"\n')

    result = run(["lab", "health"], OV_KEY="key-from-env")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert seen, "the real ov never reached the server"
    assert seen[0].get("x-api-key") == "key-from-env", seen[0]
    # And the decoy is untouched: ovx never writes to ~/.openviking.
    assert json.loads((ov_home / "ovcli.conf").read_text())["api_key"] == "key-from-home"


# --- OAuth login ---------------------------------------------------------
#
# These drive the real flow against a real HTTP server standing in for
# OpenViking, rather than mocking urllib inside the script's heredocs. The
# script's HTTP calls are the thing under test, so faking them out would leave
# the interesting half unexercised.


class FakeOpenViking(http.server.BaseHTTPRequestHandler):
    """The five OAuth endpoints ovx talks to, plus a record of what arrived."""

    seen: ClassVar[dict[str, Any]] = {}
    approve_after: int = 0
    # When set, the approved redirect carries a state ovx never sent, which is
    # what a cross-session or forged response would look like.
    corrupt_state: bool = False

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode())

    def do_GET(self) -> None:
        """Serve metadata, the authorize redirect, the page, and the status poll."""
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        base = f"http://{self.headers['Host']}"

        if parsed.path == "/.well-known/oauth-authorization-server":
            self._json(
                200,
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "registration_endpoint": f"{base}/register",
                    "revocation_endpoint": f"{base}/revoke",
                },
            )
        elif parsed.path == "/authorize":
            self.seen["authorize"] = {k: v[0] for k, v in query.items()}
            self.send_response(302)
            self.send_header("Location", f"{base}/oauth/authorize/page?pending=PEND123")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif parsed.path == "/oauth/authorize/page":
            self.seen["page_pending"] = query.get("pending", [""])[0]
            self._send(
                200,
                b'<div class="code" id="displayCode">K7MPQ2</div>',
                "text/html",
            )
        elif parsed.path == "/oauth/authorize/page/status":
            polls = self.seen.get("polls", 0) + 1
            self.seen["polls"] = polls
            if polls <= type(self).approve_after:
                self._json(200, {"status": "pending"})
                return
            state = self.seen.get("authorize", {}).get("state", "")
            if type(self).corrupt_state:
                state = "not-the-state-ovx-sent"
            redirect = self.seen.get("authorize", {}).get("redirect_uri", "")
            self._json(
                200,
                {
                    "status": "approved",
                    "redirect_url": f"{redirect}?code=AUTHCODE&state={state}",
                },
            )
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        """Serve client registration, token exchange, and revocation."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/register":
            self.seen["register"] = json.loads(raw)
            self._json(201, {"client_id": "client-abc"})
        elif parsed.path == "/token":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            self.seen.setdefault("token", []).append(form)
            grant = form.get("grant_type")
            suffix = "refreshed" if grant == "refresh_token" else "first"
            self._json(
                200,
                {
                    "access_token": f"ovat_{suffix}",
                    "refresh_token": f"ovrt_{suffix}",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        elif parsed.path == "/revoke":
            self.seen["revoke"] = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            self._json(200, {})
        else:
            self._json(404, {"error": "not_found"})

    def log_message(self, *args: object) -> None:
        """Keep the server quiet; pytest captures enough already."""


@pytest.fixture
def oauth_server() -> Iterator[tuple[str, dict[str, Any]]]:
    """Serve a stand-in OpenViking OAuth server; yield its base URL and record."""
    FakeOpenViking.seen = {}
    FakeOpenViking.approve_after = 0
    FakeOpenViking.corrupt_state = False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenViking)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", FakeOpenViking.seen
    finally:
        server.shutdown()
        server.server_close()


def token_path(workspace: Path, name: str = "lab") -> Path:
    """Where ovx stores the login for a profile."""
    return workspace / "ovx" / "tokens" / f"{name}.json"


def test_login_stores_a_token(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """A full login writes the access and refresh tokens to a private file."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')

    result = run(["--login", "lab"])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    stored = json.loads(token_path(workspace).read_text())
    assert stored["access_token"] == "ovat_first"
    assert stored["refresh_token"] == "ovrt_first"
    assert stored["client_id"] == "client-abc"
    assert stored["expires_at"] > time.time()


def test_login_token_file_is_private(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """The stored token is readable only by its owner."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')

    run(["--login", "lab"])

    mode = stat.S_IMODE(token_path(workspace).stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_login_prints_the_code_and_where_to_enter_it(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """The operator is told the six-character code and the Studio URL."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')

    result = run(["--login", "lab"])

    assert "K7MPQ2" in result.stderr, result.stderr
    assert f"{url}/studio/oauth/verify" in result.stderr, result.stderr


def test_login_uses_pkce_s256(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """The challenge is the S256 digest of the verifier sent at token exchange.

    OAuth 2.1 forbids ``plain``, and a challenge that does not match its
    verifier would be accepted by a server that never checks — so the test
    recomputes it rather than trusting the method parameter alone.
    """
    url, seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')

    run(["--login", "lab"])

    assert seen["authorize"]["code_challenge_method"] == "S256"
    verifier = seen["token"][0]["code_verifier"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert seen["authorize"]["code_challenge"] == expected


def test_login_polls_until_approved(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """A status that is not yet approved is polled again, not treated as failure."""
    url, seen = oauth_server
    FakeOpenViking.approve_after = 1
    write_config(config, f'["lab"]\nurl = "{url}"\n')

    result = run(["--login", "lab"])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert seen["polls"] >= 2, seen
    assert token_path(workspace).exists()


def test_stored_token_becomes_the_api_key(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """After login, ov is handed the OAuth token in the api_key field."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')
    run(["--login", "lab"])

    result = run(["lab", "health"])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert shim_log(workspace).config["api_key"] == "ovat_first"


def test_stored_token_outranks_the_profile_api_key(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """A logged-in profile ignores its own api_key rather than sending a stale one."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\napi_key = "static-key"\n')
    run(["--login", "lab"])

    run(["lab", "health"])

    assert shim_log(workspace).config["api_key"] == "ovat_first"


def test_expired_token_is_refreshed_before_use(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """An expired access token is exchanged for a fresh one, and the file updated."""
    url, seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')
    run(["--login", "lab"])

    stored = token_path(workspace)
    record = json.loads(stored.read_text())
    record["expires_at"] = int(time.time()) - 10
    stored.write_text(json.dumps(record))

    result = run(["lab", "health"])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert shim_log(workspace).config["api_key"] == "ovat_refreshed"
    grants = [form["grant_type"] for form in seen["token"]]
    assert "refresh_token" in grants, grants
    # The rotated refresh token replaces the old one, or the next refresh fails.
    assert json.loads(stored.read_text())["refresh_token"] == "ovrt_refreshed"


def test_unexpired_token_is_not_refreshed(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """A live token is used as-is; ovx does not spend a round trip per command."""
    url, seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')
    run(["--login", "lab"])

    run(["lab", "health"])

    assert [form["grant_type"] for form in seen["token"]] == ["authorization_code"]


def test_logout_revokes_and_removes_the_token(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """Logout tells the server to revoke, then deletes the local file."""
    url, seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')
    run(["--login", "lab"])

    result = run(["--logout", "lab"])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not token_path(workspace).exists()
    assert seen["revoke"]["token"] == "ovrt_first"
    assert seen["revoke"]["token_type_hint"] == "refresh_token"


def test_logout_removes_the_token_when_the_server_is_gone(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """A server that cannot be reached must not leave a token undeletable."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')
    run(["--login", "lab"])

    stored = token_path(workspace)
    record = json.loads(stored.read_text())
    # Port 9 is discard: it refuses or blackholes, so the revoke cannot succeed.
    record["revocation_endpoint"] = "http://127.0.0.1:9/revoke"
    stored.write_text(json.dumps(record))

    result = run(["--logout", "lab"])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert not stored.exists()


def test_logout_without_a_login_is_not_an_error(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """Logging out twice is the same as logging out once."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')

    result = run(["--logout", "lab"])

    assert result.returncode == 0, result.stderr
    assert "no stored login" in result.stderr


def test_login_and_logout_do_not_need_ov(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """Neither runs ov, so neither should require it to be installed."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')
    (workspace / "bin" / "ov").unlink()

    assert run(["--login", "lab"]).returncode == 0
    assert run(["--logout", "lab"]).returncode == 0


def test_login_rejects_a_mismatched_state(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """A redirect carrying someone else's state is refused, not exchanged."""
    url, seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')
    FakeOpenViking.corrupt_state = True

    result = run(["--login", "lab"])

    assert result.returncode != 0
    assert "state mismatch" in result.stderr, result.stderr
    assert not token_path(workspace).exists()
    # And it stopped before spending the code, rather than exchanging first.
    assert "token" not in seen, seen


def test_login_on_unknown_profile_is_rejected(
    workspace: Path, config: Path, oauth_server: tuple[str, dict[str, Any]]
) -> None:
    """A typo names a profile that does not exist rather than starting a flow."""
    url, _seen = oauth_server
    write_config(config, f'["lab"]\nurl = "{url}"\n')

    result = run(["--login", "nope"])

    assert result.returncode != 0
    assert "not found" in result.stderr


# --- install.sh ----------------------------------------------------------

INSTALLER = Path(__file__).resolve().parent.parent / "install.sh"

# What the GitHub releases API returns: newest first, prereleases mixed in,
# and other packages' releases alongside ovx's. Compact like the real body.
RELEASES_JSON = (
    '[{"id":3,"author":{"login":"someone"},"tag_name":"ovx-v0.2.0",'
    '"draft":false,"prerelease":true,"assets":[{"id":9,"name":"ovx.sh"}]},'
    '{"id":2,"author":{"login":"someone"},"tag_name":"v0.2.0",'
    '"draft":false,"prerelease":false,"assets":[]},'
    '{"id":1,"author":{"login":"someone"},"tag_name":"ovx-v0.1.0",'
    '"draft":false,"prerelease":false,"assets":[{"id":7,"name":"ovx.sh"}]}]'
)

# Stands in for curl. Answers the releases API from $FAKE_RELEASES and serves
# any download as a script that reports the version out of its own URL, so a
# test can see which release the installer chose.
CURL_SHIM = """#!/usr/bin/env bash
url=""
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
if [[ "$url" == *api.github.com* ]]; then
  cat "$FAKE_RELEASES"
  exit 0
fi
version="${url#*/download/ovx-v}"
version="${version%/ovx.sh}"
body="#!/usr/bin/env bash
echo \\"ovx $version\\""
if [ -n "$out" ]; then printf '%s\\n' "$body" > "$out"; else printf '%s\\n' "$body"; fi
"""


@pytest.fixture
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH with a curl that serves canned releases. Returns the install dir."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(CURL_SHIM)
    curl.chmod(0o755)

    releases = tmp_path / "releases.json"
    releases.write_text(RELEASES_JSON)

    target = tmp_path / "target"
    monkeypatch.setenv("FAKE_RELEASES", str(releases))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return target


def run_installer(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run install.sh with the current environment."""
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_default_install_skips_prereleases(installer: Path) -> None:
    """A bare install gets the newest stable release, not a newer beta.

    The releases endpoint returns prereleases too, so "pre-release" on GitHub
    only means something if the installer reads the flag. Without this, cutting
    a beta silently changes what everyone's `curl | bash` hands them.
    """
    result = run_installer(["--to", str(installer)])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "installed ovx 0.1.0" in result.stderr, result.stderr


def test_default_install_ignores_other_packages_releases(installer: Path) -> None:
    """A stable release of another package in this repo is not an ovx version."""
    result = run_installer(["--to", str(installer)])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    # v0.2.0 is ov-postgres', and sorts newest of the stable ones.
    assert "0.2.0" not in result.stderr.split("downloading")[1].split("\n")[0]


def test_explicit_version_installs_the_prerelease(installer: Path) -> None:
    """--version is how a beta is opted into, and it bypasses the filter."""
    result = run_installer(["--to", str(installer), "--version", "0.2.0"])

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "installed ovx 0.2.0" in result.stderr, result.stderr


def test_no_stable_release_is_a_clear_error(
    installer: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every ovx release is a prerelease, say so instead of installing one."""
    only_beta = tmp_path / "beta-only.json"
    only_beta.write_text(
        '[{"id":3,"author":{"login":"someone"},"tag_name":"ovx-v0.2.0",'
        '"draft":false,"prerelease":true,"assets":[]}]'
    )
    monkeypatch.setenv("FAKE_RELEASES", str(only_beta))

    result = run_installer(["--to", str(installer)])

    assert result.returncode != 0
    assert "found no published ovx-v* release" in result.stderr
    assert "--version" in result.stderr
