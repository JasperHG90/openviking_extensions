"""ovx against a real Vault, a real ``ov`` and a real OpenViking.

The rest of the suite stands a shim in for ``ov`` and a fake in for Vault,
which proves ovx writes and sends what it means to. It cannot prove the other
side agrees: that Vault's userpass login and identity-token mint answer the
way ovx reads them, that the ``ov`` of the server's release reads the
``ovcli.conf`` ovx materializes, and that the server takes the token ovx put in
its ``api_key`` slot. These tests run the whole chain.

They start Vault and OpenViking in containers (see ``live_openviking``) and
copy ``ov`` out of the same image, so the CLI and the server are one release.
``OV_TEST_IMAGE`` picks the image and ``OV_TEST_OV_BIN`` overrides the binary.
Marked ``integration`` and skipped without docker::

    uv run pytest -m integration
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from live_openviking import LiveOpenViking, docker_available, openviking_stack, ov_binary
from test_ovx import run, run_pty, token_path, write_config

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def stack() -> Iterator[LiveOpenViking]:
    """Vault and OpenViking, shared by every test in the module."""
    if not docker_available():
        pytest.skip("needs docker to start Vault and OpenViking")
    with openviking_stack("ovx-it") as live:
        yield live


@pytest.fixture(scope="session")
def real_ov(stack: LiveOpenViking, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy the ``ov`` binary out of the image under test."""
    return ov_binary(stack, tmp_path_factory.mktemp("ov-bin"))


@pytest.fixture
def workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stack: LiveOpenViking,
    real_ov: Path,
) -> Path:
    """Give the test a private HOME, the real ``ov`` on PATH, and the test Vault.

    ``VAULT_ADDR`` is overwritten rather than inherited: a developer's shell
    commonly points it at a real Vault, and this suite logs in.
    """
    home = tmp_path / "home"
    (home / ".openviking").mkdir(parents=True)
    # ov refuses every command until it knows a display language, and reads it
    # from here whatever OPENVIKING_CLI_CONFIG_FILE says.
    (home / ".openviking" / "ovcli.settings.conf").write_text('{"language":"en"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OVX_DIR", str(tmp_path / "ovx"))
    monkeypatch.setenv("OVX_CONFIG_FILE", str(tmp_path / "config.toml"))
    monkeypatch.setenv("VAULT_ADDR", stack.vault_url)
    monkeypatch.setenv("VAULT_TOKEN_FILE", str(tmp_path / "vault-token"))
    for name in ("VAULT_TOKEN", "VAULT_NAMESPACE", "OVX_VAULT_ROLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OPENVIKING_CLI_CONFIG_FILE", raising=False)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [str(real_ov.parent), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
        ),
    )
    return tmp_path


def lab_profile(workspace: Path, stack: LiveOpenViking) -> None:
    """Write a profile shaped like the README's: url, account, user, no key."""
    write_config(
        workspace / "config.toml",
        f'["lab"]\nurl = "{stack.url}"\naccount = "{stack.account}"\n'
        f'user = "{stack.vault_user}"\n',
    )


def log_in(stack: LiveOpenViking) -> str:
    """Log in through the password prompt, as an operator would."""
    return run_pty(
        ["--login", "--no-bind", "lab"],
        [("Vault username", "\n"), ("Password", f"{stack.vault_password}\n")],
        timeout=60,
    )


def claims(token: str) -> dict[str, object]:
    """Decode a JWT's payload without checking it; the server does that."""
    payload = token.split(".")[1]
    decoded: dict[str, object] = json.loads(base64.urlsafe_b64decode(payload + "=="))
    return decoded


def test_a_vault_login_is_what_lets_ov_in(workspace: Path, stack: LiveOpenViking) -> None:
    """Log in, then run ``ov`` through ovx as the user the token names."""
    lab_profile(workspace, stack)
    uri = f"viking://~/resources/ovx-it-{uuid.uuid4().hex[:8]}"

    before = run(["lab", "mkdir", uri])
    assert before.returncode != 0, "the server took a request with no credential"

    out = log_in(stack)
    assert "logged in" in out, out
    stored = json.loads(token_path(workspace).read_text())
    assert claims(stored["token"])["ov_account"] == stack.account

    made = run(["lab", "mkdir", uri])
    assert made.returncode == 0, made.stderr
    # The listing, not `stat`: only `ls` prints the uri the server resolved
    # `~` to on every release, and that uri is who the server thinks we are.
    listed = run(["lab", "ls", "viking://~/resources"])
    assert listed.returncode == 0, listed.stderr
    name = uri.rsplit("/", 1)[1]
    assert f"viking://user/{stack.user}/resources/{name}" in listed.stdout


def test_a_bound_profile_lets_bare_ov_in(workspace: Path, stack: LiveOpenViking) -> None:
    """``--bind`` writes a file the release's own ``ov`` reads with no ovx around."""
    lab_profile(workspace, stack)
    assert "logged in" in log_in(stack)

    bound = run(["--bind", "lab"])
    assert bound.returncode == 0, bound.stderr

    bare = subprocess.run(
        ["ov", "ls", "viking://~"],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )
    assert bare.returncode == 0, bare.stderr
    assert f"viking://user/{stack.user}" in bare.stdout
