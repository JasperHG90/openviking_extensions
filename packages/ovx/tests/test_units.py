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
