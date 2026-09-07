"""Tests for the ovx command line.

The suite drives the installed console entrypoint as a subprocess, so it
tests what an operator runs. A shim on PATH stands in for ``ov`` and
records what it saw — its argv, ``$OPENVIKING_CLI_CONFIG_FILE``, and the
contents and permissions of the file that variable pointed at — so the tests
can assert on the config ovx materialized without ovx having to expose it.

Interactive paths (the wizard, the menus) read from ``/dev/tty``, so those
tests drive the script through a pty.
"""

from __future__ import annotations

import base64
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
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

# The console entrypoint, not the retired ovx.sh. Resolved from this
# interpreter's own bin directory so the suite drives the package it was
# installed from rather than whatever `ovx` happens to be on PATH.
OVX = Path(sys.executable).parent / "ovx"

# The package's own source, for the tests that assert a whole mechanism is
# gone. Reading OVX would only read the generated console wrapper.
SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "ovx"


def _package_source() -> str:
    """Return every line of the package's source, concatenated."""
    return "\n".join(p.read_text() for p in sorted(SOURCE_DIR.glob("*.py")))


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
    # Kill before the blocking wait: a child still sitting on /dev/tty because
    # a prompt never matched would otherwise hang the whole suite here rather
    # than failing the one test.
    try:
        if os.waitpid(pid, os.WNOHANG)[0] != pid:
            os.kill(pid, signal.SIGKILL)
    except (ChildProcessError, ProcessLookupError):
        pass
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
    """-h and -V answer before any dependency check, so they always work.

    A PATH holding only this interpreter -- no ov, nothing else -- because
    both flags are what an operator reaches for when the install is broken.
    """
    env = dict(os.environ, PATH=str(Path(sys.executable).parent))

    for flag in ("-h", "--help"):
        result = subprocess.run(
            [str(OVX), flag], env=env, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout, result.stdout
        assert "ovx" in result.stdout

    result = subprocess.run(
        [str(OVX), "-V"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"ovx \S+\n", result.stdout), result.stdout


def test_help_reports_the_config_file_in_force(workspace: Path, config: Path) -> None:
    """The header documents the default path; --help names the one in use."""
    result = run(["--help"])
    assert result.returncode == 0, result.stderr
    # Click wraps the epilog, so match the path rather than the whole line.
    assert "Config in use" in result.stdout, result.stdout
    assert config.name in result.stdout, result.stdout


def test_every_option_is_documented(workspace: Path) -> None:
    """Each flag the parser accepts appears in --help.

    KNOWN_OPTIONS is what rejects a typo'd flag, and the help is generated
    from the command signature -- two different places. This is what stops
    them drifting apart.
    """
    from ovx.cli import KNOWN_OPTIONS

    help_text = run(["--help"]).stdout
    # Typer's own completion flags are listed under a separate heading that
    # click may wrap; the rest are ovx's and must all appear. Named exactly,
    # not by prefix: a `--install-*` prefix would also excuse ovx's own
    # --install-firefox-host from ever being checked.
    ours = KNOWN_OPTIONS - {"--install-completion", "--show-completion"}
    missing = [option for option in sorted(ours) if option not in help_text]
    assert not missing, f"undocumented: {missing}\n{help_text}"


def test_every_declared_option_is_known(workspace: Path) -> None:
    """Each flag the command declares is one the parser will accept.

    The other direction from the test above, and the one that bites: options
    are declared on the command signature, but `reject_unknown_options` runs
    before click sees them and refuses anything not in KNOWN_OPTIONS. Adding a
    flag and forgetting the list makes it unusable, while --help advertises it.
    """
    import typer.main

    from ovx.cli import KNOWN_OPTIONS, app

    command = typer.main.get_command(app)
    # Anything spelled with a leading dash is a flag. Not `isinstance(param,
    # click.Option)`: typer vendors its own click, so its TyperOption does not
    # subclass the top-level one and that check silently matches nothing —
    # which is exactly how this test passed while being vacuous.
    declared = {
        opt
        for param in command.params
        for opt in [*param.opts, *(param.secondary_opts or [])]
        if opt.startswith("-")
    }
    assert declared, "found no options at all; this test is not checking anything"

    unusable = sorted(declared - KNOWN_OPTIONS)
    assert not unusable, f"declared but rejected at parse time: {unusable}"


def test_help_carries_the_reference_sections(workspace: Path) -> None:
    """--help is the manual, as the shell version's header block was.

    Losing these to a generated one-line description is a real regression:
    they are the only place the Vault variables and the no-revoke caveat are
    written down at the point of use.
    """
    help_text = run(["--help"]).stdout
    for needle in (
        "OVX_VAULT_ROLE",
        "VAULT_SKIP_VERIFY",
        "VAULT_TOKEN_HELPER",
        "api_key = ",
        "ovx lab find",
        "no server-side revoke",
    ):
        assert needle in help_text, f"{needle!r} missing from --help"


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


# --- Vault identity-token login ------------------------------------------
#
# ovx no longer speaks OAuth. OpenViking's oidc auth mode never builds an
# api_key manager, and its oauth_verify refuses to mint without one, so DCR,
# PKCE and the ovat_ flow are not degraded there but refused outright. Logging
# in is now: ask Vault for an identity token, put it in the api_key slot.
#
# These drive the real script against a shim `vault` on PATH that records its
# argv, the same way the suite stands in for `ov`. The shim is the boundary
# ovx actually depends on, so faking anything nearer would leave the
# interesting half unexercised.


def make_jwt(expires_in: int = 3600, account: str = "jasper") -> str:
    """Build a token shaped like Vault's, with a real ``exp``.

    Only the shape matters here: ovx never verifies the signature, and must
    not — the server does that. What it does read is ``exp``, and that it has
    exactly two dots, which is how OpenViking recognizes a JWT in the api_key
    slot.

    Parameters
    ----------
    expires_in :
        Seconds from now until the token expires. Negative for an expired one.
    account :
        Value of the ``ov_account`` claim the server maps identity from.

    Returns
    -------
    str
        A three-segment JWT.
    """

    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = segment({"alg": "RS256", "typ": "JWT"})
    body = segment(
        {
            "iss": "https://vault.example.com/v1/identity/oidc",
            "aud": "openviking",
            "sub": "351f302a-0000-0000-0000-000000000000",
            "ov_account": account,
            "exp": int(time.time()) + expires_in,
        }
    )
    return f"{header}.{body}.signaturenotchecked"


def token_path(workspace: Path, name: str = "lab") -> Path:
    """Where ovx stores the login for a profile."""
    return workspace / "ovx" / "tokens" / f"{name}.json"


def write_token(workspace: Path, token: str, name: str = "lab") -> Path:
    """Place a stored login on disk, as a previous run would have left it."""
    path = token_path(workspace, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==").decode())
    path.write_text(
        json.dumps(
            {
                "profile": name,
                "kind": "vault-identity",
                "role": "openviking",
                "token": token,
                "expires_at": body["exp"],
            }
        )
    )
    path.chmod(0o600)
    return path


class FakeVault(http.server.BaseHTTPRequestHandler):
    """The three Vault endpoints ovx talks to, plus a record of what arrived.

    ovx speaks to Vault over HTTP rather than shelling out to the ``vault``
    CLI, so the HTTP calls are the thing under test. Standing up a real server
    exercises the headers, the JSON shapes and the status handling; mocking
    urllib inside the heredocs would leave all of that unchecked.
    """

    seen: ClassVar[dict[str, Any]] = {}
    valid_session: ClassVar[bool] = False
    password: ClassVar[str] = "hunter2"
    token: ClassVar[str] = ""
    mint_status: ClassVar[int] = 200

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, name: str) -> None:
        self.seen.setdefault(name, []).append(
            {
                "path": self.path,
                "token": self.headers.get("X-Vault-Token"),
                "namespace": self.headers.get("X-Vault-Namespace"),
            }
        )

    def do_GET(self) -> None:
        """Serve token lookup and identity-token minting."""
        if self.path == "/v1/auth/token/lookup-self":
            self._record("lookup")
            if type(self).valid_session and self.headers.get("X-Vault-Token"):
                self._send(200, {"data": {"id": "s.session"}})
            else:
                self._send(403, {"errors": ["permission denied"]})
        elif self.path.startswith("/v1/identity/oidc/token/"):
            self._record("mint")
            if not type(self).valid_session:
                self._send(403, {"errors": ["permission denied"]})
            elif type(self).mint_status != 200:
                self._send(type(self).mint_status, {"errors": ["no such role"]})
            else:
                self._send(200, {"data": {"token": type(self).token}})
        else:
            self._send(404, {"errors": ["unsupported path"]})

    def do_POST(self) -> None:
        """Serve userpass login."""
        if not self.path.startswith("/v1/auth/userpass/login/"):
            self._send(404, {"errors": ["unsupported path"]})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.seen.setdefault("login", []).append(
            {"path": self.path, "password": body.get("password")}
        )
        if body.get("password") != type(self).password:
            self._send(400, {"errors": ["invalid username or password"]})
            return
        type(self).valid_session = True
        self._send(200, {"auth": {"client_token": "s.session"}})

    def log_message(self, *args: object) -> None:
        """Keep the server quiet; the tests assert on ovx's output."""


@pytest.fixture
def vault(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeVault]]:
    """Run a fake Vault and point $VAULT_ADDR at it.

    Starts with no session, and with the token helper redirected into the
    workspace so no test can read or write the real ``~/.vault-token``.
    """
    FakeVault.seen = {}
    FakeVault.valid_session = False
    FakeVault.password = "hunter2"
    FakeVault.token = make_jwt()
    FakeVault.mint_status = 200

    server = http.server.HTTPServer(("127.0.0.1", 0), FakeVault)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setenv("VAULT_ADDR", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("VAULT_TOKEN_FILE", str(workspace / "vault-token"))
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    try:
        yield FakeVault
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def give_session(workspace: Path, vault: type[FakeVault]) -> None:
    """Pretend the operator already has a live Vault session."""
    vault.valid_session = True
    helper = workspace / "vault-token"
    helper.write_text("s.session")
    helper.chmod(0o600)


def test_login_mints_and_stores_a_token(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """With a live Vault session, login mints and stores without asking anything."""
    write_config(config, LAB)
    give_session(workspace, vault)

    result = run(["--login", "lab"])

    assert result.returncode == 0, result.stderr
    stored = json.loads(token_path(workspace).read_text())
    assert stored["token"] == vault.token
    assert stored["kind"] == "vault-identity"
    assert stored["role"] == "openviking"
    # The expiry comes out of the JWT, not from a TTL ovx assumed.
    payload = json.loads(base64.urlsafe_b64decode(vault.token.split(".")[1] + "=="))
    assert stored["expires_at"] == payload["exp"]


def test_login_reads_the_identity_token_role(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """The mint is a read of the role's identity-token path, with the session."""
    write_config(config, LAB)
    give_session(workspace, vault)

    run(["--login", "lab"])

    assert vault.seen["mint"][0]["path"] == "/v1/identity/oidc/token/openviking"
    assert vault.seen["mint"][0]["token"] == "s.session"


def test_login_honors_a_custom_role(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """$OVX_VAULT_ROLE picks the role, since the role fixes aud and ov_account."""
    write_config(config, LAB)
    give_session(workspace, vault)

    run(["--login", "lab"], OVX_VAULT_ROLE="openviking-staging")

    assert vault.seen["mint"][0]["path"].endswith("/openviking-staging")


def test_login_sends_the_vault_namespace_when_set(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """$VAULT_NAMESPACE reaches Vault, so a namespaced lab works unchanged."""
    write_config(config, LAB)
    give_session(workspace, vault)

    run(["--login", "lab"], VAULT_NAMESPACE="team-a")

    assert vault.seen["mint"][0]["namespace"] == "team-a"


def test_login_token_file_is_private(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """The stored token is readable only by its owner."""
    write_config(config, LAB)
    give_session(workspace, vault)

    run(["--login", "lab"])

    assert stat.S_IMODE(token_path(workspace).stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path(workspace).parent.stat().st_mode) == 0o700


def test_login_without_vault_addr_says_so(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Without $VAULT_ADDR ovx has no Vault to call, so it refuses before trying."""
    write_config(config, LAB)

    result = run(["--login", "lab"], VAULT_ADDR="")

    assert result.returncode == 1
    assert "VAULT_ADDR" in result.stderr
    assert not token_path(workspace).exists()


def test_login_needs_no_vault_cli(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Ovx speaks to Vault itself; the vault binary is not a dependency.

    PATH here holds only the ov shim, so if ovx shelled out to `vault` this
    would fail rather than mint.
    """
    write_config(config, LAB)
    give_session(workspace, vault)

    # A PATH holding the ov shim, a python3 new enough for tomllib, and the
    # system directories the script needs for bash and coreutils — but no
    # `vault`. If ovx shelled out to it, this would fail. The interpreter comes
    # before /usr/bin so python3 is 3.12 rather than the 3.9 macOS ships.
    interpreter = str(Path(sys.executable).parent)
    path = os.pathsep.join([str(workspace / "bin"), interpreter, "/usr/bin", "/bin"])
    assert shutil.which("vault", path=path) is None

    result = run(["--login", "lab"], PATH=path)

    assert result.returncode == 0, result.stderr
    assert token_path(workspace).exists()


def test_login_without_a_session_or_terminal_says_so(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Logging in needs a password prompt, which needs a terminal.

    Minting with an existing session does not, so the check belongs on this
    branch rather than around the whole command.
    """
    write_config(config, LAB)

    result = run(["--login", "lab"])

    assert result.returncode == 1
    assert "no terminal to log in from" in result.stderr


def test_login_through_a_terminal_authenticates_then_mints(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """With no session, ovx posts the password to Vault and then mints."""
    write_config(config, '["lab"]\nurl = "https://ov.example.com"\nuser = "jasper"\n')

    out = run_pty(
        ["--login", "lab"],
        [("Vault username", "\n"), ("Password", "hunter2\n")],
    )

    assert "logged in" in out, out
    assert vault.seen["login"][0]["path"] == "/v1/auth/userpass/login/jasper"
    assert "mint" in vault.seen
    assert token_path(workspace).exists()


def test_login_caches_the_session_token_privately(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """The Vault session is cached where the CLI keeps its own, at 0600."""
    write_config(config, '["lab"]\nurl = "https://ov.example.com"\nuser = "jasper"\n')

    run_pty(
        ["--login", "lab"],
        [("Vault username", "\n"), ("Password", "hunter2\n")],
    )

    helper = workspace / "vault-token"
    assert helper.read_text().strip() == "s.session"
    assert stat.S_IMODE(helper.stat().st_mode) == 0o600


def test_password_never_reaches_the_command_line(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """The password goes in the request body, never argv or the environment."""
    write_config(config, '["lab"]\nurl = "https://ov.example.com"\nuser = "jasper"\n')

    out = run_pty(
        ["--login", "lab"],
        [("Vault username", "\n"), ("Password", "hunter2\n")],
    )

    assert vault.seen["login"][0]["password"] == "hunter2"
    # And it is not echoed back to the terminal.
    assert "hunter2" not in out, out


def test_failed_vault_login_stores_nothing(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Bad credentials leave no token behind."""
    write_config(config, '["lab"]\nurl = "https://ov.example.com"\nuser = "jasper"\n')

    out = run_pty(
        ["--login", "lab"],
        [("Vault username", "\n"), ("Password", "wrong\n")],
    )

    assert "Vault login failed" in out, out
    assert not token_path(workspace).exists()
    assert not (workspace / "vault-token").exists()


def test_mint_failure_is_reported_and_stores_nothing(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A role the entity cannot read is named rather than silently skipped."""
    write_config(config, LAB)
    give_session(workspace, vault)
    vault.mint_status = 500

    result = run(["--login", "lab"])

    assert result.returncode == 1
    assert "could not mint" in result.stderr
    assert not token_path(workspace).exists()


def test_empty_mint_is_refused(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Vault answering 200 with no token must not store an empty credential."""
    write_config(config, LAB)
    give_session(workspace, vault)
    vault.token = ""

    result = run(["--login", "lab"])

    assert result.returncode == 1
    assert not token_path(workspace).exists()


def test_token_without_an_exp_is_refused(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Without exp ovx cannot re-mint ahead of time, so it will not store it."""
    write_config(config, LAB)
    give_session(workspace, vault)
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(b'{"aud":"openviking"}').rstrip(b"=").decode()
    vault.token = f"{header}.{body}.sig"

    result = run(["--login", "lab"])

    assert result.returncode == 1
    assert "exp" in result.stderr
    assert not token_path(workspace).exists()


# --- using a stored token -------------------------------------------------


def test_stored_token_becomes_the_api_key(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """The JWT travels in api_key: ov has no bearer field and needs none.

    OpenViking's _extract_token treats a two-dot api_key as a JWT, so the
    transport is unchanged from the api-key case.
    """
    write_config(config, LAB)
    token = make_jwt()
    write_token(workspace, token)

    run(["lab", "status"], OV_KEY="from-the-profile")

    assert shim_log(workspace).config["api_key"] == token
    assert token.count(".") == 2


def test_stored_token_outranks_the_profile_api_key(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A logged-in profile ignores its own api_key rather than sending a stale one."""
    write_config(config, LAB)
    write_token(workspace, make_jwt())

    run(["lab", "status"], OV_KEY="static-key")

    assert shim_log(workspace).config["api_key"] != "static-key"


def test_live_token_is_not_reminted(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A token with time left is used as-is; no Vault round trip per command."""
    write_config(config, LAB)
    write_token(workspace, make_jwt(expires_in=3600))

    run(["lab", "status"], OV_KEY="unused")

    assert "mint" not in vault.seen


@pytest.mark.parametrize(
    ("expires_in", "reminted"),
    [
        (3600, False),  # comfortably live
        (600, False),  # outside the 300s window
        (120, True),  # inside it
        (-10, True),  # already gone
    ],
)
def test_the_renewal_window_is_five_minutes(
    expires_in: int,
    reminted: bool,
    workspace: Path,
    config: Path,
    vault: type[FakeVault],
) -> None:
    """Pin the skew both sides, so narrowing or widening it is a test failure.

    Too narrow and a long ov command outlives its token; too wide and every
    run pays for a mint it did not need.
    """
    write_config(config, LAB)
    write_token(workspace, make_jwt(expires_in=expires_in))
    give_session(workspace, vault)

    run(["lab", "status"], OV_KEY="unused")

    assert ("mint" in vault.seen) is reminted


def test_expiring_token_is_reminted_before_use(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A token near expiry is replaced silently, and the file updated.

    Identity tokens cannot be refreshed — there is no grant to exchange — so
    renewal is a fresh mint against the live Vault session.
    """
    write_config(config, LAB)
    write_token(workspace, make_jwt(expires_in=30))
    give_session(workspace, vault)
    vault.token = make_jwt(expires_in=3600, account="jasper")
    fresh = vault.token

    result = run(["lab", "status"], OV_KEY="unused")

    assert result.returncode == 0, result.stderr
    assert shim_log(workspace).config["api_key"] == fresh
    assert json.loads(token_path(workspace).read_text())["token"] == fresh


def test_remint_is_silent(workspace: Path, config: Path, vault: type[FakeVault]) -> None:
    """Renewal must be invisible while the Vault session is alive."""
    write_config(config, LAB)
    write_token(workspace, make_jwt(expires_in=30))
    give_session(workspace, vault)

    result = run(["lab", "status"], OV_KEY="unused")

    assert result.stderr.strip() == "", result.stderr


def test_expired_token_without_a_session_says_run_login(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A lapsed Vault session gets an instruction, not an obscure failure."""
    write_config(config, LAB)
    write_token(workspace, make_jwt(expires_in=-10))

    result = run(["lab", "status"], OV_KEY="unused")

    assert result.returncode != 0
    # The message names what actually failed rather than always blaming the
    # session: a bad $VAULT_CACERT or an unreachable Vault used to send the
    # operator to `ovx --login`, which then failed identically.
    assert "cannot be renewed" in result.stderr, result.stderr
    assert "ovx --login lab" in result.stderr


def test_expired_token_without_vault_addr_is_explained(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Losing $VAULT_ADDR after logging in explains it rather than 401ing."""
    write_config(config, LAB)
    write_token(workspace, make_jwt(expires_in=-10))

    result = run(["lab", "status"], OV_KEY="unused", VAULT_ADDR="")

    assert result.returncode != 0
    assert "VAULT_ADDR is not set" in result.stderr


# --- a login that exists but cannot be used -------------------------------
#
# Every case here must ABORT, never fall back to the profile's api_key.
# Falling back would quietly swap a short-lived identity token for a
# long-lived static key resolving to a different caller, and the only visible
# sign would be a different identity on the server.


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("corrupt json", "{ broken"),
        ("empty file", ""),
        ("a list, not an object", "[]"),
        ("a bare string", '"hello"'),
        ("token is not a string", '{"token": 123, "expires_at": 99999999999}'),
        ("no token at all", '{"expires_at": 99999999999}'),
        ("expiry is not a number", '{"token": "a.b.c", "expires_at": "soon"}'),
        ("no expiry", '{"token": "a.b.c"}'),
    ],
)
def test_an_unusable_token_file_aborts_and_never_downgrades(
    name: str,
    body: str,
    workspace: Path,
    config: Path,
    vault: type[FakeVault],
) -> None:
    """A broken login is refused, with a message and no traceback."""
    write_config(config, LAB)
    path = token_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)

    result = run(["lab", "status"], OV_KEY="static-fallback-key")

    assert result.returncode != 0, f"{name}: should not have succeeded"
    assert not shim_log(workspace).ran, f"{name}: ov ran with the fallback key"
    assert "Traceback" not in result.stderr, f"{name}: {result.stderr}"
    assert "ovx --login" in result.stderr, f"{name}: {result.stderr}"


def test_oauth_era_token_file_is_rejected_with_advice(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A token stored by the OAuth build cannot work and says what to do.

    Its ovat_ token is not a JWT and OpenViking's OAuth now answers 503, so
    carrying on would fail later with a bare error.
    """
    write_config(config, LAB)
    path = token_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "profile": "lab",
                "access_token": "ovat_old",
                "refresh_token": "ovrt_old",
                "expires_at": int(time.time()) + 3600,
            }
        )
    )

    result = run(["lab", "status"], OV_KEY="static-fallback-key")

    assert result.returncode != 0
    assert "predates Vault identity tokens" in result.stderr
    assert not shim_log(workspace).ran


# --- logout ---------------------------------------------------------------


def test_logout_removes_the_token_and_calls_nothing(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """There is nothing to revoke, so logout is a local delete and no more.

    Revoking the Vault session would destroy it for every other tool on the
    machine, and an identity token has no server-side revoke of its own.
    """
    write_config(config, LAB)
    write_token(workspace, make_jwt())
    give_session(workspace, vault)

    result = run(["--logout", "lab"])

    assert result.returncode == 0, result.stderr
    assert not token_path(workspace).exists()
    assert vault.seen == {}
    # The Vault session itself survives.
    assert (workspace / "vault-token").exists()


def test_logout_without_a_login_is_not_an_error(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Logging out twice is the same as logging out once."""
    write_config(config, LAB)

    result = run(["--logout", "lab"])

    assert result.returncode == 0
    assert "no stored login" in result.stderr


def test_logout_leaves_other_profiles_alone(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """One profile's logout does not touch another's stored token."""
    write_config(
        config,
        '["lab"]\nurl = "https://ov.example.com"\n'
        '["prod"]\nurl = "https://ov.example.org"\n',
    )
    write_token(workspace, make_jwt(), name="lab")
    write_token(workspace, make_jwt(), name="prod")

    run(["--logout", "lab"])

    assert not token_path(workspace, "lab").exists()
    assert token_path(workspace, "prod").exists()


@pytest.mark.parametrize("evil", ["../../pwned", "a/b", ".", ".."])
def test_a_profile_name_that_is_a_path_is_refused(
    evil: str, workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A profile name is a TOML key, so it can be anything the operator types.

    Interpolated straight into the token path, ``ovx --logout '../../x'``
    would rm -f outside the token directory.
    """
    write_config(config, f'["{evil}"]\nurl = "https://ov.example.com"\n')
    canary = workspace / "pwned.json"
    canary.write_text("do not delete")

    result = run(["--logout", evil], OVX_DIR=str(workspace / "ovx"))

    assert result.returncode != 0
    assert "must not contain a path" in result.stderr
    assert canary.exists()


def test_after_logout_a_keyless_profile_says_how_to_log_back_in(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Ov's own 401 does not mention logging in, so ovx does.

    This is the state right after --logout on an oidc server: no token, and no
    api_key to fall back to.
    """
    write_config(config, '["lab"]\nurl = "https://ov.example.com"\n')
    write_token(workspace, make_jwt())
    run(["--logout", "lab"])

    result = run(["lab", "status"])

    assert "no stored login" in result.stderr
    assert "ovx --login lab" in result.stderr


def test_a_profile_with_a_key_and_no_login_stays_quiet(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """The hint is for the keyless case; a $VAR profile is working as designed."""
    write_config(config, LAB)

    result = run(["lab", "status"], OV_KEY="a-real-key")

    assert "no stored login" not in result.stderr


# --- shape of the command ------------------------------------------------


def test_login_and_logout_do_not_need_ov(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """Neither runs ov, so neither should require it to be installed."""
    write_config(config, LAB)
    give_session(workspace, vault)
    (workspace / "bin" / "ov").unlink()

    assert run(["--login", "lab"]).returncode == 0
    assert run(["--logout", "lab"]).returncode == 0


def test_login_on_unknown_profile_is_rejected(
    workspace: Path, config: Path, vault: type[FakeVault]
) -> None:
    """A typo names a profile that does not exist rather than reaching Vault."""
    write_config(config, LAB)
    give_session(workspace, vault)

    result = run(["--login", "nope"])

    assert result.returncode == 1
    assert "profile 'nope' not found" in result.stderr
    assert vault.seen == {}


def test_no_oauth_machinery_remains() -> None:
    """The OAuth flow is gone, not merely unused.

    OpenViking's OAuth subsystem answers 503 on every endpoint, so a leftover
    call would be a guaranteed failure rather than a fallback.
    """
    source = _package_source()
    for dead in (
        "/oauth/authorize",
        "/.well-known/oauth-authorization-server",
        "registration_endpoint",
        "code_challenge",
        "code_verifier",
        "refresh_token",
        "token_endpoint",
        "revocation_endpoint",
        "studio/oauth",
    ):
        assert dead not in source, f"{dead} still appears in the package"


def test_the_vault_cli_is_not_a_dependency() -> None:
    """Ovx must not shell out to `vault`; it speaks the HTTP API itself."""
    source = _package_source()
    for dead in ("command -v vault", "vault token lookup", "vault read -field"):
        assert dead not in source, f"{dead} still appears in the package"
