"""Talking to Vault.

``ovx`` speaks Vault's HTTP API directly rather than shelling out to the
``vault`` CLI: it needs three endpoints, and requiring a second CLI to run this
one is a poor trade for the JSON parsing it would save.

What the CLI does own is the *token helper* — the file it caches a session
token in. That is ``~/.vault-token``, a plain file, so this module reads and
writes it directly and the two share one session in either direction. A custom
``$VAULT_TOKEN_HELPER`` is not supported; set ``$VAULT_TOKEN`` instead.
"""

from __future__ import annotations

import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ovx.errors import OvxError
from ovx.fs import write_private

DEFAULT_ROLE = "openviking"
DEFAULT_TIMEOUT = 30.0

# Go's strconv.ParseBool, which is what Vault itself uses for VAULT_SKIP_VERIFY.
# Matching it exactly matters in both directions: "t" must disable verification
# because Vault would, and "yes" must NOT, because Vault rejects it and a user
# who typed it is not asking to run unverified.
_GO_TRUE = frozenset({"1", "t", "T", "TRUE", "true", "True"})

# Vault responses are small. A cap keeps a hostile or broken endpoint from
# making ovx read an unbounded body into memory.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    ``urlopen`` follows redirects by default and re-sends the headers, so a
    Vault that answers ``302 Location: http://elsewhere`` would send
    ``X-Vault-Token`` — a live session token — to another host, in cleartext,
    even when ``$VAULT_CACERT`` pins the real one. Worse, the redirect target's
    body would then be accepted as Vault's answer, letting it choose the JWT
    ovx stores and hands to ``ov``.

    Vault's own standby forwarding is transparent to clients, so nothing ovx
    calls legitimately needs a redirect.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        """Return ``None``, which tells urllib not to follow."""
        return None


def _opener() -> urllib.request.OpenerDirector:
    """Return an opener that talks to Vault and follows nothing."""
    return urllib.request.build_opener(
        _NoRedirects, urllib.request.HTTPSHandler(context=_ssl_context())
    )


class VaultError(OvxError):
    """Vault refused a request, or could not be reached."""


def _env(name: str) -> str:
    """Return a stripped environment variable, or an empty string."""
    return os.environ.get(name, "").strip()


def role() -> str:
    """Return the Vault role to mint from.

    The role fixes the token's audience and its ``ov_account`` claim, so
    minting from the wrong one authenticates as a different identity. It is
    overridable because a staging instance needs a different one.
    """
    return _env("OVX_VAULT_ROLE") or DEFAULT_ROLE


def address() -> str:
    """Return ``$VAULT_ADDR`` without its trailing slash.

    Returns
    -------
    str
        The configured Vault address, empty when unset.
    """
    return os.environ.get("VAULT_ADDR", "").rstrip("/")


def token_helper_path() -> Path:
    """Return the file the Vault session token is cached in."""
    return Path(_env("VAULT_TOKEN_FILE") or "~/.vault-token").expanduser()


def session_token() -> str:
    """Return the current Vault session token, or an empty string.

    ``$VAULT_TOKEN`` wins over the cached file, matching the CLI.
    """
    token = _env("VAULT_TOKEN")
    if token:
        return token
    try:
        return token_helper_path().read_text().strip()
    except OSError:
        return ""


def _ssl_context() -> ssl.SSLContext:
    """Return the TLS context for talking to Vault.

    Honors ``$VAULT_SKIP_VERIFY`` and ``$VAULT_CACERT`` so a lab behind a
    private CA works without ovx inventing its own names for either.
    """
    if _env("VAULT_SKIP_VERIFY") in _GO_TRUE:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return ssl.create_default_context(cafile=_env("VAULT_CACERT") or None)


def request(
    path: str,
    *,
    payload: dict[str, object] | None = None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, object]:
    """Call Vault and return the decoded body.

    Parameters
    ----------
    path :
        API path below ``/v1``, e.g. ``"auth/token/lookup-self"``.
    payload :
        Body to POST as JSON. A GET is sent when omitted.
    token :
        Session token for ``X-Vault-Token``. The cached one is used when
        omitted; pass ``""`` to send none.
    timeout :
        Seconds to wait.

    Returns
    -------
    dict[str, object]
        The parsed response body.

    Raises
    ------
    VaultError
        On a transport failure or a non-2xx status, carrying Vault's own
        message where it sent one.
    """
    addr = address()
    if not addr:
        raise VaultError("$VAULT_ADDR is not set")

    headers = {"Accept": "application/json"}
    auth = session_token() if token is None else token
    if auth:
        headers["X-Vault-Token"] = auth
    namespace = _env("VAULT_NAMESPACE")
    if namespace:
        headers["X-Vault-Namespace"] = namespace

    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    call = urllib.request.Request(f"{addr}/v1/{path}", data=data, headers=headers)
    try:
        with _opener().open(call, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise VaultError("Vault sent an implausibly large response")
    except urllib.error.HTTPError as error:
        raise VaultError(f"HTTP {error.code}{_error_detail(error)}") from None
    except (urllib.error.URLError, OSError) as error:
        raise VaultError(str(error)) from None

    try:
        decoded = json.loads(body or b"{}")
    except ValueError as error:
        raise VaultError(f"Vault sent a body that is not JSON: {error}") from None
    if not isinstance(decoded, dict):
        raise VaultError("Vault sent a body that is not an object")
    return decoded


def _error_detail(error: urllib.error.HTTPError) -> str:
    """Return Vault's own error text for an HTTP failure, if it sent any."""
    try:
        body = json.loads(error.read() or b"{}")
        message = body.get("errors", [""])[0] if isinstance(body, dict) else ""
    except (ValueError, OSError, IndexError, AttributeError):
        return ""
    return f": {message}" if message else ""


def check_session() -> None:
    """Raise unless the cached session token still works.

    Separate from :func:`session_is_live` because the *reason* matters when a
    renewal fails. Collapsing every cause to "your session is gone" sends the
    operator to ``ovx --login``, which then fails the same way — while the
    actual cause was a wrong ``$VAULT_CACERT``, an unreachable Vault, or a
    hung connection.

    Raises
    ------
    VaultError
        Carrying what actually went wrong.
    """
    if not address():
        raise VaultError("$VAULT_ADDR is not set")
    if not session_token():
        raise VaultError("no Vault session token")
    request("auth/token/lookup-self")


def session_is_live() -> bool:
    """Whether the cached session token still works.

    Minting needs a session; this is how ovx decides whether to ask for a
    password. Use :func:`check_session` when the reason for a failure has to
    reach the operator.
    """
    try:
        check_session()
    except VaultError:
        return False
    return True


def log_in(user: str, *, password: str | None = None) -> str:
    """Exchange a password for a Vault session token and cache it.

    The password is read here rather than accepted as an argument by default,
    so it never reaches ``argv``, the environment, or a log. ``getpass`` reads
    from the terminal with echo off, which also means it cannot be piped in —
    deliberate, since a piped password lands in shell history or a CI log.

    Parameters
    ----------
    user :
        Vault username for the userpass method.
    password :
        Supplied only by tests. Left unset, it is prompted for.

    Returns
    -------
    str
        The session token, which is also cached unless ``$VAULT_TOKEN`` is set.

    Raises
    ------
    VaultError
        If no password is given, or Vault refuses the login.
    """
    if password is None:
        try:
            password = getpass.getpass(f"Password for {user}: ", stream=sys.stderr)
        except (OSError, EOFError):
            raise VaultError("could not read a password from the terminal") from None
    if not password:
        raise VaultError("no password given")

    try:
        body = request(
            f"auth/userpass/login/{urllib.parse.quote(user, safe='')}",
            payload={"password": password},
            token="",
        )
    except VaultError as error:
        raise VaultError(f"Vault login failed ({error})") from None
    auth = body.get("auth")
    token = auth.get("client_token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        raise VaultError("Vault accepted the login but returned no token")

    # Skipped when $VAULT_TOKEN is set: that is the caller's session to manage,
    # not a file for ovx to overwrite.
    if not _env("VAULT_TOKEN"):
        try:
            write_private(token_helper_path(), token)
        except OSError as error:
            raise VaultError(
                f"logged in but could not cache the token: {error}"
            ) from None
    return token


def mint(role: str = DEFAULT_ROLE, *, token: str | None = None) -> str:
    """Mint an OpenViking identity token.

    The result is an RS256 JWT carrying the ``ov_account`` claim the server
    maps identity from. Its lifetime comes from the role, so ovx never states
    one — callers read the ``exp`` the token actually carries.

    The issuer is deliberately not checked. It has to match the server's
    configuration byte for byte and is set on the Vault side, so validating it
    here would only add a second place to get it wrong.

    Parameters
    ----------
    role :
        Vault identity-token role to mint from.
    token :
        Session token to mint with. Pass the one a fresh ``log_in`` returned:
        left unset, this falls back to ``session_token()``, which prefers
        ``$VAULT_TOKEN`` — so a stale exported token would be used instead of
        the session just established, and the mint would 403 after the user
        had already typed a valid password.

    Returns
    -------
    str
        The signed token.

    Raises
    ------
    VaultError
        If Vault refuses, or returns no token.
    """
    try:
        body = request(
            f"identity/oidc/token/{urllib.parse.quote(role, safe='')}", token=token
        )
    except VaultError as error:
        raise VaultError(f"could not mint from role {role!r} ({error})") from None
    data = body.get("data")
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise VaultError(f"Vault returned no token for role {role!r}")
    return token
