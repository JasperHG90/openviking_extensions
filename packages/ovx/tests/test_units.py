"""Unit tests for the modules the end-to-end suite exercises only indirectly.

A review mutated the source and found that 24 of 29 changes left the suite
green — including deleting the temp-directory cleanup outright, making
``materialize`` ignore the stored token, and turning every ``0600`` into
``0644``. Those all live in ``runner``, ``config``, ``fs`` and ``paths``,
which the subprocess tests reach only through their happy paths.

These pin the invariants directly, so a mutation has to fail something.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ovx import tokens
from ovx.cli import KNOWN_OPTIONS, reject_unknown_options, split_argv
from ovx.config import Profile, banner, expand, load_profile, materialize
from ovx.errors import OvxError
from ovx.fs import ensure_private_dir, write_private
from ovx.paths import Locations
from ovx.runner import private_workspace

# --- fs: every secret is 0600 from creation ------------------------------


def test_write_private_is_owner_only(tmp_path: Path) -> None:
    """A credential must never exist world-readable, not even briefly."""
    target = tmp_path / "nested" / "secret.json"
    write_private(target, "s3cret")
    assert target.read_text() == "s3cret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_private_leaves_no_temp_behind(tmp_path: Path) -> None:
    """A failed write must not litter the directory with partial credentials."""
    target = tmp_path / "sub" / "secret.json"
    target.parent.mkdir()
    target.parent.chmod(0o500)  # readable and searchable, not writable
    try:
        with pytest.raises(OSError):
            write_private(target, "s3cret")
        assert list(target.parent.iterdir()) == []
    finally:
        target.parent.chmod(0o700)


def test_write_private_replaces_atomically(tmp_path: Path) -> None:
    """Rewriting keeps the mode and the new content."""
    target = tmp_path / "secret.json"
    write_private(target, "one")
    write_private(target, "two")
    assert target.read_text() == "two"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_ensure_private_dir_is_owner_only(tmp_path: Path) -> None:
    """The token directory holds credentials, so it is 0700."""
    target = tmp_path / "tokens"
    ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


# --- runner: the temp workspace is private and always removed ------------


def test_private_workspace_is_owner_only_and_removed() -> None:
    """The whole point of ovx: the credential's directory is private, and goes."""
    with private_workspace() as root:
        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        seen = root
    assert not seen.exists()


def test_private_workspace_is_removed_on_an_exception() -> None:
    """A failure part-way through must not leave the config behind."""
    seen = None
    with pytest.raises(RuntimeError), private_workspace() as root:
        seen = root
        (root / "ovcli.conf").write_text("secret")
        raise RuntimeError("boom")
    assert seen is not None
    assert not seen.exists()


# --- config: expansion, typing, and the token override -------------------


def _profile(**fields: object) -> Profile:
    """Build a profile without going through a file."""
    return Profile(name="lab", fields={"url": "https://x", **fields})


def test_expand_resolves_a_bare_and_braced_var() -> None:
    """Both spellings expand, since a config may use either."""
    out = expand(_profile(api_key="$K", account="${A}"), {"K": "secret", "A": "acme"})
    assert out["api_key"] == "secret"
    assert out["account"] == "acme"


def test_expand_walks_nested_tables() -> None:
    """A variable inside extra_headers expands too."""
    out = expand(_profile(extra_headers={"X-Tok": "$K"}), {"K": "secret"})
    assert out["extra_headers"] == {"X-Tok": "secret"}


@pytest.mark.parametrize("environ", [{}, {"K": ""}])
def test_expand_refuses_an_unset_var(environ: dict[str, str]) -> None:
    """A missing secret fails loudly rather than sending an empty key.

    Returning "" here would run against a half-built config -- the failure
    mode this whole mechanism exists to avoid.
    """
    with pytest.raises(OvxError) as caught:
        expand(_profile(api_key="$K"), environ)
    assert caught.value.code == 4


def test_expand_leaves_a_literal_alone() -> None:
    """A profile may hold a literal key; ovx discourages it but honors it."""
    out = expand(_profile(api_key="literal"), {})
    assert out["api_key"] == "literal"


def test_materialize_is_owner_only_and_refuses_an_existing_path(
    tmp_path: Path,
) -> None:
    """O_EXCL, and 0600 from the descriptor rather than a later chmod."""
    dest = tmp_path / "ovcli.conf"
    materialize(_profile(api_key="literal"), dest, environ={})
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    with pytest.raises(OvxError):
        materialize(_profile(), dest, environ={})


def test_materialize_applies_the_stored_token(tmp_path: Path) -> None:
    """The token outranks the profile's api_key.

    Skipping this is the silent downgrade: ov would authenticate with the
    long-lived static key under a different server identity.
    """
    dest = tmp_path / "ovcli.conf"
    written = materialize(_profile(api_key="static"), dest, token="a.b.c", environ={})
    assert written["api_key"] == "a.b.c"
    assert json.loads(dest.read_text())["api_key"] == "a.b.c"


def test_materialize_keeps_the_api_key_when_there_is_no_token(
    tmp_path: Path,
) -> None:
    """No login means the profile's own key still stands."""
    dest = tmp_path / "ovcli.conf"
    written = materialize(_profile(api_key="static"), dest, environ={})
    assert written["api_key"] == "static"


def test_banner_masks_the_key() -> None:
    """The banner goes to a terminal, where scrollback outlives the command."""
    line = banner("lab", {"url": "https://x", "api_key": "sk-abcdefghijklmnop"})
    assert "sk-abcdefghijklmnop" not in line
    assert "lab" in line and "https://x" in line


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('["lab"]\nurl = "https://x"\nnope = "y"\n', 2),  # unknown field
        ('["lab"]\napi_key = "k"\n', 2),  # no url
        ('["lab"]\nurl = "https://x"\ntimeout = "soon"\n', 2),  # wrong type
        ('["lab"]\nurl = "https://x"\ntimeout = true\n', 2),  # bool is not a num
    ],
)
def test_load_profile_rejects_a_bad_profile(body: str, code: int, tmp_path: Path) -> None:
    """A config error exits 2, which scripts distinguish from other failures."""
    config = tmp_path / "config.toml"
    config.write_text(body)
    with pytest.raises(OvxError) as caught:
        load_profile(config, "lab")
    assert caught.value.code == code


# --- paths ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected_root", "expected_config"),
    [
        ({"HOME": "/h"}, "/h/.ovx", "/h/.ovx/config.toml"),
        ({"HOME": "/h", "OVX_DIR": "/d"}, "/d", "/d/config.toml"),
        ({"HOME": "/h", "OVX_CONFIG_FILE": "/c.toml"}, "/h/.ovx", "/c.toml"),
    ],
)
def test_locations_resolve(
    environ: dict[str, str], expected_root: str, expected_config: str
) -> None:
    """$OVX_CONFIG_FILE overrides only the config, not the token directory."""
    found = Locations.resolve(environ)
    assert str(found.root) == expected_root
    assert str(found.config_file) == expected_config
    assert str(found.token_dir) == f"{expected_root}/tokens"


def test_locations_never_resolve_relative() -> None:
    """An empty $HOME must not put a live token in the working directory."""
    assert Locations.resolve({"HOME": ""}).root.is_absolute()


# --- cli argv handling ---------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "ovx_args", "ov_args"),
    [
        ([], [], []),
        (["-l"], ["-l"], []),
        (["lab"], ["lab"], []),
        (["lab", "status"], ["lab"], ["status"]),
        (["lab", "-o", "json"], ["lab"], ["-o", "json"]),
        # An ovx flag after the profile belongs to ov.
        (["lab", "-e"], ["lab"], ["-e"]),
        (["lab", "--login"], ["lab"], ["--login"]),
        # A leading -- means "no profile; the rest is ov's".
        (["--", "-o", "json"], [], ["-o", "json"]),
        # One -- after the profile is dropped, a second is forwarded.
        (["lab", "--", "-o"], ["lab"], ["-o"]),
        (["lab", "--", "--", "s"], ["lab"], ["--", "s"]),
        (["-e", "lab"], ["-e", "lab"], []),
    ],
)
def test_split_argv(argv: list[str], ovx_args: list[str], ov_args: list[str]) -> None:
    """Option parsing stops at the first bare word, which is the profile."""
    found = split_argv(argv)
    assert found.ovx_args == ovx_args
    assert found.ov_args == ov_args


def test_every_known_option_is_really_an_option() -> None:
    """KNOWN_OPTIONS is what rejects a typo, so it must not drift into prose."""
    assert all(option.startswith("-") for option in KNOWN_OPTIONS)


@pytest.mark.parametrize("bad", ["--lst", "-Z", "--login-now", "--lst=x"])
def test_reject_unknown_options(bad: str) -> None:
    """A typo'd flag is named, not silently taken as a profile."""
    with pytest.raises(OvxError) as caught:
        reject_unknown_options([bad])
    assert "unknown option" in str(caught.value)


@pytest.mark.parametrize("good", ["-l", "--list", "--logout", "-L", "lab", "-"])
def test_accept_known_options(good: str) -> None:
    """Real options, and a bare '-', pass through."""
    reject_unknown_options([good])


# --- tokens: expiry ------------------------------------------------------


def test_is_stale_uses_the_five_minute_window() -> None:
    """Pinned both sides: too narrow dies mid-command, too wide mints always."""
    now = time.time()
    fresh = tokens.StoredLogin("lab", "r", "a.b.c", now + 600)
    expiring = tokens.StoredLogin("lab", "r", "a.b.c", now + 120)
    assert not fresh.is_stale(now=now)
    assert expiring.is_stale(now=now)


def test_save_reads_the_expiry_from_the_token(tmp_path: Path) -> None:
    """The role's TTL can change server-side; exp is what is enforced."""
    import base64

    def segment(payload: dict[str, object]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        )

    expires = int(time.time()) + 4242
    token = f"{segment({'alg': 'RS256'})}.{segment({'exp': expires})}.sig"
    stored = tokens.save(tmp_path, "lab", token, "openviking")
    assert stored.expires_at == expires
    assert stat.S_IMODE((tmp_path / "lab.json").stat().st_mode) == 0o600


def test_save_refuses_a_token_with_no_expiry(tmp_path: Path) -> None:
    """Without exp ovx cannot renew ahead of time, so it will not store it."""
    with pytest.raises(OvxError):
        tokens.save(tmp_path, "lab", "not.a.jwt", "openviking")
    assert not (tmp_path / "lab.json").exists()


# --- end to end: the token never reaches ov's environment ----------------


def test_ov_does_not_inherit_the_token(tmp_path: Path) -> None:
    """Only the temp config carries it; an exported one is readable by anyone."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "ov"
    shim.write_text(
        '#!/usr/bin/env bash\nenv | grep -i "token\\|api_key" > "$OVX_ENV_DUMP" || true\n'
    )
    shim.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('["lab"]\nurl = "https://x"\n')
    dump = tmp_path / "env.txt"

    token_dir = tmp_path / "ovx" / "tokens"
    token_dir.mkdir(parents=True)
    import base64

    def segment(payload: dict[str, object]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        )

    secret = (
        f"{segment({'alg': 'RS256'})}."
        f"{segment({'exp': int(time.time()) + 3600})}.SECRETSIG"
    )
    tokens.save(token_dir, "lab", secret, "openviking")

    subprocess.run(
        [str(Path(sys.executable).parent / "ovx"), "lab", "status"],
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "OVX_DIR": str(tmp_path / "ovx"),
            "OVX_CONFIG_FILE": str(config),
            "OVX_ENV_DUMP": str(dump),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "SECRETSIG" not in dump.read_text(), dump.read_text()


# --- the five mutations a review found unpinned ---------------------------


def _signal_fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Build a workspace whose fake ov sleeps, and report the config path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "ov"
    shim.write_text(
        '#!/usr/bin/env bash\necho "$OPENVIKING_CLI_CONFIG_FILE" > "$CONFECHO"\n'
        "sleep 30\n"
    )
    shim.chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('["lab"]\nurl = "https://x"\napi_key = "$K"\n')
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OVX_DIR": str(tmp_path / "ovx"),
        "OVX_CONFIG_FILE": str(config),
        "CONFECHO": str(tmp_path / "conf"),
        "K": "LEAKED-SECRET",
    }
    return tmp_path / "conf", env


@pytest.mark.parametrize(
    "signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=lambda s: s.name
)
def test_the_credential_is_removed_on_every_trapped_signal(
    signum: signal.Signals, tmp_path: Path
) -> None:
    """Not just SIGINT.

    TERM and HUP end the interpreter outright under Python's defaults, so
    without a handler each leaves a live token on disk permanently. A review
    found dropping them from _TRAPPED left every test green.
    """
    echo, env = _signal_fixture(tmp_path)
    process = subprocess.Popen(
        [str(Path(sys.executable).parent / "ovx"), "lab", "status"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while not echo.exists() or not echo.read_text().strip():
        if time.time() > deadline:
            process.kill()
            pytest.fail("ov never started")
        time.sleep(0.05)

    process.send_signal(signum)
    # A second signal is the case that used to abort the removal itself. The
    # process may already be gone, which is fine.
    try:
        process.send_signal(signum)
    except (ProcessLookupError, OSError):
        pass
    process.wait(timeout=20)

    conf = Path(echo.read_text().strip())
    assert not conf.exists(), f"{signum.name} left the credential at {conf}"
    assert not conf.parent.exists(), f"{signum.name} left {conf.parent}"


def _calls_named(module: str, dotted: str) -> bool:
    """Whether ``module`` really calls ``dotted``, ignoring comments and text.

    A plain substring search matches the word inside a docstring explaining
    why the call was removed, which is exactly backwards.
    """
    import ast

    source = (
        Path(__file__).resolve().parent.parent / "src" / "ovx" / f"{module}.py"
    ).read_text()
    wanted = dotted.split(".")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        parts: list[str] = []
        target: ast.expr = node.func
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if list(reversed(parts)) == wanted:
            return True
    return False


def test_the_wizard_prompts_do_not_read_stdin() -> None:
    """typer.prompt reads stdin, so a pipe could answer its own confirmation.

    Both halves were regressions the shell version did not have: the api_key
    was displayed and echoed, and `yes | ovx -d prod` deleted a profile.
    """
    assert not _calls_named("wizard", "typer.prompt")
    assert not _calls_named("wizard", "typer.confirm")
    assert _calls_named("wizard", "getpass.getpass"), "secrets are not hidden"


def test_the_username_prompt_does_not_read_stdin() -> None:
    """A pipe must not choose whose password the operator is asked for."""
    assert not _calls_named("cli", "typer.prompt")


def test_a_piped_answer_cannot_delete_a_profile(tmp_path: Path) -> None:
    """The end-to-end half: stdin is not a terminal, so the wizard refuses."""
    config = tmp_path / "config.toml"
    config.write_text('["lab"]\nurl = "https://x"\n')
    before = config.read_text()

    result = subprocess.run(
        [str(Path(sys.executable).parent / "ovx"), "-d", "lab"],
        env={
            **os.environ,
            "OVX_DIR": str(tmp_path / "ovx"),
            "OVX_CONFIG_FILE": str(config),
        },
        input="y\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert config.read_text() == before, "a piped 'y' deleted the profile"


def test_the_banner_goes_to_stderr(tmp_path: Path) -> None:
    """On stdout it would corrupt whatever is parsing ov's output."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ov").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "ov").chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('["lab"]\nurl = "https://x"\napi_key = "$K"\n')

    result = subprocess.run(
        [str(Path(sys.executable).parent / "ovx"), "lab", "status"],
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "OVX_DIR": str(tmp_path / "ovx"),
            "OVX_CONFIG_FILE": str(config),
            "K": "a-key",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "profile=" not in result.stdout, result.stdout


def test_the_vault_token_file_is_not_clobbered_when_the_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$VAULT_TOKEN is the caller's session to manage, not a file to overwrite."""
    from ovx import vault

    helper = tmp_path / "vault-token"
    helper.write_text("s.pre-existing")
    monkeypatch.setenv("VAULT_TOKEN_FILE", str(helper))
    monkeypatch.setenv("VAULT_TOKEN", "s.from-the-env")
    monkeypatch.setenv("VAULT_ADDR", "http://127.0.0.1:1")

    with pytest.raises(vault.VaultError):
        vault.log_in("jasper", password="hunter2")
    assert helper.read_text() == "s.pre-existing"


def test_help_shows_no_click_escape_markers() -> None:
    r"""``\b`` is click's do-not-rewrap marker and must never be visible.

    It has to be the real escape (ASCII backspace), so the command docstring
    cannot be a raw string. Ruff's D301 asks for ``r\"\"\"`` on any docstring
    holding a backslash; complying turned the marker into two literal
    characters and printed it eleven times in ``ovx --help``.
    """
    from ovx.cli import main

    doc = main.__doc__
    assert doc is not None, "main lost its docstring, which is ovx's whole manual"
    assert "\\x08" in doc or "\b" in doc, (
        "the docstring lost click's marker -- is it a raw string again?"
    )
    assert "\\\\b" not in repr(doc), "the marker is literal, not an escape"


def test_help_keeps_its_line_breaks(tmp_path: Path) -> None:
    """The Behavior block is a table; rewrapped into a paragraph it is useless."""
    config = tmp_path / "config.toml"
    config.write_text('["lab"]\nurl = "https://x"\n')
    result = subprocess.run(
        [str(Path(sys.executable).parent / "ovx"), "-h"],
        env={
            **os.environ,
            "OVX_DIR": str(tmp_path / "ovx"),
            "OVX_CONFIG_FILE": str(config),
            "COLUMNS": "100",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "\\b" not in result.stdout, "click's marker is being printed literally"
    # Each example must still be on its own line.
    for example in ("ovx lab find", "ovx -e lab", "ovx -- -o json status"):
        assert any(example in line for line in result.stdout.splitlines()), (
            f"{example!r} was rewrapped away"
        )


def test_the_vault_username_is_a_prompt_not_a_silent_default() -> None:
    """The profile's ``user`` is offered, never used outright.

    OpenViking stopped reading that field for identity, so it drifts: it may
    be a display name, or differ in case from the Vault account. Vault's
    userpass is case-sensitive, so using it silently means typing a real
    password at a prompt for the wrong account.
    """
    import inspect

    from ovx import cli

    source = inspect.getsource(cli._vault_username)
    assert "default=default" in source, "the profile value is not a default"
    # The old shape returned early when the profile had a user, skipping the
    # prompt entirely.
    assert "if user:\n        return user" not in source


def test_a_secret_is_passed_exactly_as_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trimming a password turns a correct one into a bare 401.

    Ordinary answers are stripped -- nobody means the newline after a profile
    name -- but a secret is whatever was typed.
    """
    from ovx import wizard

    monkeypatch.setattr("getpass.getpass", lambda _prompt: "  pa ss  ")
    assert wizard.ask("api_key", secret=True) == "  pa ss  "


def test_an_empty_secret_keeps_the_current_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter on an api_key prompt means "leave it alone", not "blank it"."""
    from ovx import wizard

    monkeypatch.setattr("getpass.getpass", lambda _prompt: "")
    assert wizard.ask("api_key", secret=True, default="$OLD") == "$OLD"


# --- bind: the deliberate exception to the temp-file rule ----------------


def _bind_workspace(tmp_path: Path) -> dict[str, str]:
    """Build an ovx home with one profile and an ov config path of its own."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ov").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "ov").chmod(0o755)
    config = tmp_path / "config.toml"
    config.write_text('["lab"]\nurl = "https://ov.example.com"\napi_key = "$K"\n')
    return {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "OVX_DIR": str(tmp_path / "ovx"),
        "OVX_CONFIG_FILE": str(config),
        "OPENVIKING_CLI_CONFIG_FILE": str(tmp_path / "ovhome" / "ovcli.conf"),
        "K": "secret-from-env",
    }


def _run_ovx(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed entrypoint with ``env``."""
    return subprocess.run(
        [str(Path(sys.executable).parent / "ovx"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_bind_writes_a_config_ov_can_use_alone(tmp_path: Path) -> None:
    """The point of bind: something other than ovx can now reach the profile.

    $VAR is expanded, because a bare ov has no idea what $K means.
    """
    env = _bind_workspace(tmp_path)
    target = Path(env["OPENVIKING_CLI_CONFIG_FILE"])

    result = _run_ovx(env, "--bind", "lab")

    assert result.returncode == 0, result.stderr
    written = json.loads(target.read_text())
    assert written["api_key"] == "secret-from-env"
    assert written["url"] == "https://ov.example.com"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_bind_honours_the_ov_config_env_var(tmp_path: Path) -> None:
    """It writes where ov actually looks, not always ~/.openviking."""
    env = _bind_workspace(tmp_path)
    elsewhere = tmp_path / "somewhere" / "else.conf"
    env["OPENVIKING_CLI_CONFIG_FILE"] = str(elsewhere)

    _run_ovx(env, "--bind", "lab")

    assert elsewhere.is_file()


def test_bind_says_the_credential_is_now_persistent(tmp_path: Path) -> None:
    """Quietly undoing the tool's one guarantee would be the wrong default."""
    env = _bind_workspace(tmp_path)
    result = _run_ovx(env, "--bind", "lab")
    assert "stays on disk" in result.stderr, result.stderr


def test_unbind_removes_what_bind_wrote(tmp_path: Path) -> None:
    """The loop closes: the credential can be taken off disk again."""
    env = _bind_workspace(tmp_path)
    target = Path(env["OPENVIKING_CLI_CONFIG_FILE"])
    _run_ovx(env, "--bind", "lab")
    assert target.is_file()

    result = _run_ovx(env, "--unbind")

    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_unbind_refuses_a_config_ovx_did_not_write(tmp_path: Path) -> None:
    """Ov's config path may predate ovx by years; deleting it would be rude."""
    env = _bind_workspace(tmp_path)
    target = Path(env["OPENVIKING_CLI_CONFIG_FILE"])
    target.parent.mkdir(parents=True)
    target.write_text('{"url": "https://hand.written", "api_key": "mine"}')

    result = _run_ovx(env, "--unbind")

    assert result.returncode != 0
    assert "nothing is bound" in result.stderr
    assert target.read_text().startswith('{"url": "https://hand.written"')


def test_bind_prefers_a_stored_login_over_the_api_key(tmp_path: Path) -> None:
    """Same precedence as a normal run, or bind would write a stale key."""
    import base64

    env = _bind_workspace(tmp_path)
    token_dir = Path(env["OVX_DIR"]) / "tokens"
    token_dir.mkdir(parents=True)

    def segment(payload: dict[str, object]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        )

    jwt = f"{segment({'alg': 'RS256'})}.{segment({'exp': int(time.time()) + 3600})}.sig"
    tokens.save(token_dir, "lab", jwt, "openviking")

    _run_ovx(env, "--bind", "lab")

    written = json.loads(Path(env["OPENVIKING_CLI_CONFIG_FILE"]).read_text())
    assert written["api_key"] == jwt, "bind wrote the static key over the login"


# --- login offers to bind ------------------------------------------------


def test_login_offers_to_bind_only_where_it_can_ask(tmp_path: Path) -> None:
    """No terminal means no prompt: in a script, silence is not consent."""
    import inspect

    from ovx import cli

    source = inspect.getsource(cli._offer_to_bind)
    assert "_has_tty()" in source, "the offer can fire without a terminal"


def test_the_bind_offer_defaults_to_no() -> None:
    """A stray Enter must not leave a credential on disk.

    Binding undoes the one guarantee ovx makes, so the safe answer is the
    default and saying yes is deliberate.
    """
    import inspect

    from ovx import cli

    source = inspect.getsource(cli._offer_to_bind)
    assert '("y", "yes")' in source, "the offer no longer requires an explicit yes"
    assert "[y/N]" in source, "the prompt no longer shows which way it defaults"


@pytest.mark.parametrize(
    ("args", "leftover"),
    [
        (["-L", "lab", "--no-bind"], "--no-bind"),
        (["--logout", "lab", "--bind"], "--bind"),
        (["-l", "lab", "extra"], "extra"),
    ],
)
def test_a_command_that_never_runs_ov_refuses_leftover_args(
    args: list[str], leftover: str, tmp_path: Path
) -> None:
    """Otherwise the flag lands on ov's side of the split and is discarded.

    `ovx -L lab --no-bind` prompted anyway, because --no-bind came after the
    profile name and so belonged to an ov that never ran.
    """
    config = tmp_path / "config.toml"
    config.write_text('["lab"]\nurl = "https://x"\n')
    result = subprocess.run(
        [str(Path(sys.executable).parent / "ovx"), *args],
        env={
            **os.environ,
            "OVX_DIR": str(tmp_path / "ovx"),
            "OVX_CONFIG_FILE": str(config),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "does not run ov" in result.stderr, result.stderr
    assert leftover in result.stderr
    assert "before the profile name" in result.stderr
