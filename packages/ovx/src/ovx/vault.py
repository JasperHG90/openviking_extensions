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
from dataclasses import dataclass
from pathlib import Path

from ovx.errors import OvxError
from ovx.fs import write_private

DEFAULT_ROLE = "openviking"
DEFAULT_TIMEOUT = 30.0

# A pushed second factor waits on a human tapping a phone, so the 30 seconds
# every other call gets would cut off a login that was going to succeed: Vault
# mints the token when the operator taps, and ovx would have stopped listening.
# This sits above Vault's own ceiling on purpose -- it cancels the request at
# max_request_duration, 90 seconds by default -- so the operator reads Vault's
# "push verification operation canceled" rather than a bare socket timeout, and
# a listener configured for longer still works.
PUSH_TIMEOUT = 300.0

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


@dataclass(frozen=True)
class _Challenge:
    """A second factor Vault wants before it will issue a session token.

    Attributes
    ----------
    request_id : str
        Vault's handle for the half-finished login, echoed back on validate.
    method_id : str
        Id of the MFA method to answer. It keys the passcode in the validate
        payload, so it is Vault's own id and never a name ovx picked.
    method_type : str
        Vault's name for the method, e.g. ``totp`` or ``duo``. Only used to
        say which factor is being asked for.
    uses_passcode : bool
        Whether the method is answered by typing a code. Duo, Okta and PingID
        push a prompt to the operator's device instead, and take an empty
        slice of passcodes.
    """

    request_id: str
    method_id: str
    method_type: str
    uses_passcode: bool


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


def _client_token(body: dict[str, object]) -> str:
    """Return the session token a login response carries, or an empty string."""
    auth = body.get("auth")
    token = auth.get("client_token") if isinstance(auth, dict) else None
    return token if isinstance(token, str) else ""


def _challenge(body: dict[str, object]) -> _Challenge | None:
    """Return the second factor Vault asked for, or ``None`` if it asked none.

    Vault answers an MFA-enforced login with HTTP 200 and an
    ``mfa_requirement`` where the token would be, so the challenge has to be
    read off the body — the status says nothing.

    It arrives *inside* ``auth``, next to an empty ``client_token``, and reading
    it from the top level instead is how this was broken first. A top-level one
    is accepted as a fallback because the localstack CLI, which grew this flow
    before ovx did, carries the same fallback — keeping the two clients in step
    costs one line.

    The method ids sit two levels below it, under a key that is the
    *enforcement's* name (``operator-totp`` today, anything tomorrow), so
    nothing here may key on a fixed one: hardcoding it would break login the
    day the enforcement is renamed or widened.

    Any one of a constraint's methods satisfies it, and the first is taken, as
    Vault's own CLI does.

    Parameters
    ----------
    body :
        A login response that carried no token.

    Returns
    -------
    _Challenge | None
        The one method to answer, or ``None`` when the body holds no usable
        challenge — which is a genuinely failed login.

    Raises
    ------
    VaultError
        When two enforcements match the login. Vault wants every one of them
        satisfied, so answering the first would fail with an enforcement the
        operator was never prompted for. Vault's own CLI refuses the same case.
    """
    auth = body.get("auth")
    requirement = auth.get("mfa_requirement") if isinstance(auth, dict) else None
    if not isinstance(requirement, dict):
        requirement = body.get("mfa_requirement")
    if not isinstance(requirement, dict):
        return None
    request_id = requirement.get("mfa_request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    constraints = requirement.get("mfa_constraints")
    if not isinstance(constraints, dict):
        return None
    answerable: dict[str, _Challenge] = {}
    for name, constraint in constraints.items():
        options = constraint.get("any") if isinstance(constraint, dict) else None
        if not isinstance(options, list):
            continue
        for option in options:
            method_id = option.get("id") if isinstance(option, dict) else None
            if not isinstance(method_id, str) or not method_id:
                continue
            method_type = option.get("type")
            answerable[str(name)] = _Challenge(
                request_id=request_id,
                method_id=method_id,
                method_type=method_type if isinstance(method_type, str) else "unnamed",
                # Vault omits the flag when it is false, so only an explicit
                # true means "type a code". Guessing the other way would prompt
                # for a code that does not exist.
                uses_passcode=option.get("uses_passcode") is True,
            )
            break
    if not answerable:
        return None
    if len(answerable) > 1:
        raise VaultError(
            "this login needs a second factor for each of "
            + ", ".join(sorted(answerable)),
            "ovx can answer one. Validate the login yourself with 'vault write "
            "sys/mfa/validate', or leave this mount a single login enforcement.",
        )
    return next(iter(answerable.values()))


def _answer_challenge(challenge: _Challenge, *, passcode: str | None = None) -> str:
    """Answer a second factor and return the session token it finally yields.

    What comes back is an ordinary login token, so everything downstream of
    ``log_in`` — caching it, minting with it — needs no idea this happened.

    Parameters
    ----------
    challenge :
        What Vault asked for.
    passcode :
        Supplied only by tests. Left unset, it is prompted for — and not asked
        for at all by a method that pushes to a device.

    Returns
    -------
    str
        The session token from the now-finished login.

    Raises
    ------
    VaultError
        If no passcode is given, or the validate call fails.
    """
    answers: list[str] = []
    timeout = DEFAULT_TIMEOUT
    if challenge.uses_passcode:
        if passcode is None:
            try:
                passcode = getpass.getpass("TOTP passcode: ", stream=sys.stderr)
            except (OSError, EOFError):
                raise VaultError("could not read a passcode from the terminal") from None
        # A TOTP code cannot contain a space, so surrounding whitespace is a
        # typo rather than part of the secret -- unlike a password, which is
        # sent as typed.
        passcode = passcode.strip()
        if not passcode:
            raise VaultError("no passcode given")
        answers = [passcode]
    else:
        # Duo, Okta and PingID push a prompt to the device instead of issuing a
        # code, so the slice of passcodes is empty and Vault holds the request
        # open while the operator answers there. Say so, or ovx looks hung.
        print(
            f"ovx: approve the {challenge.method_type} request on your device.",
            file=sys.stderr,
        )
        timeout = PUSH_TIMEOUT

    try:
        body = request(
            "sys/mfa/validate",
            payload={
                "mfa_request_id": challenge.request_id,
                # A list, keyed by the method id. Vault rejects a bare string
                # in a way that reads like a wrong code.
                "mfa_payload": {challenge.method_id: answers},
            },
            # This call completes a login, so it carries no token: a stale
            # cached one has no business on it.
            token="",
            timeout=timeout,
        )
    except VaultError as error:
        raise VaultError(f"could not validate the second factor ({error})") from None
    token = _client_token(body)
    if not token:
        raise VaultError("Vault accepted the passcode but returned no token")
    return token


def log_in(user: str, *, password: str | None = None, passcode: str | None = None) -> str:
    """Exchange a password, and any second factor, for a cached session token.

    The password is read here rather than accepted as an argument by default,
    so it never reaches ``argv``, the environment, or a log. ``getpass`` reads
    from the terminal with echo off, which also means it cannot be piped in —
    deliberate, since a piped password lands in shell history or a CI log.

    Where the mount enforces MFA, Vault answers the password with a challenge
    instead of a token; this satisfies it — a prompt for a code, or a wait on a
    pushed approval — and returns the same kind of token either way.

    Parameters
    ----------
    user :
        Vault username for the userpass method.
    password :
        Supplied only by tests. Left unset, it is prompted for.
    passcode :
        Supplied only by tests. Left unset, a code is prompted for when Vault
        asks for one.

    Returns
    -------
    str
        The session token, which is also cached unless ``$VAULT_TOKEN`` is set.

    Raises
    ------
    VaultError
        If no password is given, Vault refuses the login, or a second factor
        is asked for and not satisfied.
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
    token = _client_token(body)
    if not token:
        # A 200 with no token is not a failed login. On a mount with MFA
        # enforced it is how Vault asks for the second factor, so the fork is
        # on the body and never on the status.
        challenge = _challenge(body)
        if challenge is None:
            raise VaultError("Vault accepted the login but returned no token")
        token = _answer_challenge(challenge, passcode=passcode)

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
