"""Regression tests for the four defects an adversarial review found.

Each was reproduced against the shell implementation and then carried into the
Python port, because the port moved the same logic. They are grouped here
because they share one theme: a credential decision that failed *quietly*.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ovx import tokens, vault
from ovx.errors import UnusableLogin


def make_jwt(expires_in: int = 3600) -> str:
    """Build a token shaped like Vault's, with a real ``exp``."""
    import base64

    def segment(payload: dict[str, object]) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = segment({"alg": "RS256", "typ": "JWT"})
    body = segment({"aud": "openviking", "exp": int(time.time()) + expires_in})
    return f"{header}.{body}.signaturenotchecked"


# --- Finding 1: absent must not be confused with unreadable ---------------
#
# is_file() answers False for all of these, so each was read as "never logged
# in" and fell back to the profile's static api_key, with empty stderr and
# exit 0 -- the exact downgrade the three-way contract exists to prevent.


def test_unreadable_token_dir_is_not_read_as_no_login(tmp_path: Path) -> None:
    """A token directory that lost its +x bit must not look like "no login".

    This is the realistic one: a chmod -R, an archive restored without exec
    bits, or a dotfiles sync.
    """
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    tokens.save(token_dir, "lab", make_jwt(), "openviking")
    token_dir.chmod(0o600)
    try:
        with pytest.raises(UnusableLogin):
            tokens.load(token_dir, "lab")
    finally:
        token_dir.chmod(0o700)


def test_a_directory_at_the_token_path_is_unusable(tmp_path: Path) -> None:
    """Something is there; it just is not a login."""
    token_dir = tmp_path / "tokens"
    (token_dir / "lab.json").mkdir(parents=True)
    with pytest.raises(UnusableLogin):
        tokens.load(token_dir, "lab")


def test_a_dangling_symlink_is_unusable(tmp_path: Path) -> None:
    """Someone pointed the token somewhere, and it is gone."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "lab.json").symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(UnusableLogin):
        tokens.load(token_dir, "lab")


def test_a_fifo_at_the_token_path_is_unusable_and_does_not_hang(
    tmp_path: Path,
) -> None:
    """A FIFO must be classified by stat, never opened -- a read would block."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    os.mkfifo(token_dir / "lab.json")
    with pytest.raises(UnusableLogin):
        tokens.load(token_dir, "lab")


def test_a_genuinely_missing_file_is_still_no_login(tmp_path: Path) -> None:
    """The common case must stay None, or every fresh profile would abort."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    assert tokens.load(token_dir, "lab") is None


# --- Finding 4: the name guard belongs on writes, not reads ---------------


def test_a_path_shaped_profile_can_still_run(tmp_path: Path) -> None:
    """``["prod/eu"]`` is legal TOML and had no login; it must not abort.

    Refusing on the read path bricked such a profile even when it carried a
    working api_key.
    """
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    assert tokens.load(token_dir, "prod/eu") is None
    assert tokens.load(token_dir, "..") is None


@pytest.mark.parametrize("evil", ["../../pwned", "a/b", ".", "..", ""])
def test_writes_and_deletes_still_refuse_a_path(evil: str, tmp_path: Path) -> None:
    """The guard still holds where it matters: creating and removing files."""
    from ovx.errors import OvxError

    with pytest.raises(OvxError):
        tokens.token_path(tmp_path, evil)


# --- Finding 2: a redirect must not carry the session token --------------


class _Redirector(BaseHTTPRequestHandler):
    """Answers every request with a redirect to the sink."""

    sink: ClassVar[str] = ""

    def do_GET(self) -> None:
        """Redirect, cross-scheme, the way a hostile Vault would."""
        self.send_response(302)
        self.send_header("Location", type(self).sink)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Stay quiet."""


class _Sink(BaseHTTPRequestHandler):
    """Records any header it is handed."""

    seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        """Record the headers and answer with an attacker-chosen token."""
        type(self).seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps({"data": {"token": "attacker.chosen.token"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Stay quiet."""


@pytest.fixture
def redirect_pair() -> Iterator[tuple[str, type[_Sink]]]:
    """Run a redirecting "Vault" and the sink it points at."""
    _Sink.seen = []
    sink = HTTPServer(("127.0.0.1", 0), _Sink)
    _Redirector.sink = f"http://127.0.0.1:{sink.server_port}/stolen"
    front = HTTPServer(("127.0.0.1", 0), _Redirector)
    for server in (sink, front):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{front.server_port}", _Sink
    finally:
        for server in (sink, front):
            server.shutdown()
            server.server_close()


def test_a_redirect_never_forwards_the_session_token(
    redirect_pair: tuple[str, type[_Sink]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Urlopen re-sends headers across a redirect; ovx must not follow.

    Otherwise a live Vault session token leaves for another host in cleartext,
    and the redirect target's body is accepted as Vault's answer -- letting it
    choose the JWT ovx stores and hands to ov.
    """
    addr, sink = redirect_pair
    monkeypatch.setenv("VAULT_ADDR", addr)
    monkeypatch.setenv("VAULT_TOKEN", "s.super-secret-session")

    with pytest.raises(vault.VaultError):
        vault.mint("openviking")

    assert sink.seen == [], f"the session token reached the redirect target: {sink.seen}"


# --- Finding 3: a stale $VAULT_TOKEN must not waste the password ---------


class _StrictVault(BaseHTTPRequestHandler):
    """A Vault that actually validates the token it issued."""

    issued: ClassVar[str] = "s.fresh"
    minted_with: ClassVar[list[str | None]] = []

    def _json(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        """Accept the login and issue a fresh session token."""
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._json(200, {"auth": {"client_token": type(self).issued}})

    def do_GET(self) -> None:
        """Mint only for the token this server actually issued."""
        presented = self.headers.get("X-Vault-Token")
        type(self).minted_with.append(presented)
        if presented != type(self).issued:
            self._json(403, {"errors": ["permission denied"]})
            return
        self._json(200, {"data": {"token": make_jwt()}})

    def log_message(self, *args: object) -> None:
        """Stay quiet."""


def test_login_mints_with_the_session_it_just_got(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale exported $VAULT_TOKEN must not be preferred over a fresh login.

    Before the fix the user typed a valid password, Vault accepted it, and the
    mint then used the dead environment token and failed with "permission
    denied" -- blaming the role, which was the one thing that was fine.
    """
    _StrictVault.minted_with = []
    server = HTTPServer(("127.0.0.1", 0), _StrictVault)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("VAULT_ADDR", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("VAULT_TOKEN", "s.dead")

        session = vault.log_in("jasper", password="hunter2")
        assert session == "s.fresh"

        token = vault.mint("openviking", token=session)
        assert token.count(".") == 2
        assert _StrictVault.minted_with == ["s.fresh"]
    finally:
        server.shutdown()
        server.server_close()


# --- Finding 9: VAULT_SKIP_VERIFY must mean what Vault means -------------


@pytest.mark.parametrize(
    ("value", "verifies"),
    [
        # Go's strconv.ParseBool, which is what Vault uses.
        ("1", False),
        ("t", False),
        ("T", False),
        ("true", False),
        ("TRUE", False),
        ("True", False),
        # Not a Go bool: Vault rejects it, so ovx must keep verifying rather
        # than silently run unverified.
        ("yes", True),
        ("on", True),
        ("0", True),
        ("", True),
    ],
)
def test_skip_verify_matches_go_parsebool(
    value: str, verifies: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open on a value Vault would reject is the dangerous direction."""
    monkeypatch.setenv("VAULT_SKIP_VERIFY", value)
    context = vault._ssl_context()
    assert (context.verify_mode is not ssl.CERT_NONE) is verifies


def test_verification_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default must verify, whatever else is set."""
    monkeypatch.delenv("VAULT_SKIP_VERIFY", raising=False)
    assert vault._ssl_context().verify_mode is not ssl.CERT_NONE
