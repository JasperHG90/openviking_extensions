"""Stored logins: where they live, and whether one can be used.

A stored login is a Vault identity token plus the expiry read out of it. The
central distinction here is between *no login* and *a broken login*: the first
is ordinary and falls back to the profile's ``api_key``, the second must abort.
Treating them alike is how a short-lived identity token silently becomes a
long-lived static key resolving to a different caller.
"""

from __future__ import annotations

import base64
import binascii
import json
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ovx.errors import OvxError, UnusableLogin
from ovx.fs import write_private

# A token inside this window is treated as already gone. Minting costs one
# Vault call; handing ov a token that dies mid-command costs a confusing 401.
RENEWAL_SKEW_SECONDS = 300


@dataclass(frozen=True)
class StoredLogin:
    """A token ovx minted earlier, and what it knows about it.

    Attributes
    ----------
    profile : str
        Profile the token was minted for.
    role : str
        Vault role it came from.
    token : str
        The signed JWT.
    expires_at : float
        Unix time the token stops being valid, from its own ``exp`` claim.
    """

    profile: str
    role: str
    token: str
    expires_at: float

    def is_stale(self, *, now: float | None = None) -> bool:
        """Whether the token should be replaced before use."""
        moment = time.time() if now is None else now
        return moment + RENEWAL_SKEW_SECONDS >= self.expires_at


def is_safe_name(profile: str) -> bool:
    """Whether a profile name can be used as a filename.

    A profile is a TOML key, so it can be anything the operator types, and the
    README invites hand-editing the config. ``["prod/eu"]`` is legal TOML.
    """
    return (
        bool(profile)
        and profile not in (".", "..")
        and not any(character in profile for character in "/\\")
    )


def token_path(token_dir: Path, profile: str) -> Path:
    """Return the file a profile's login lives in.

    Parameters
    ----------
    token_dir :
        Directory holding one file per profile.
    profile :
        Profile name.

    Returns
    -------
    Path
        The token file.

    Raises
    ------
    OvxError
        If the name would escape ``token_dir``. Interpolated blindly,
        ``ovx --logout '../../.ssh/id_ed25519'`` would delete exactly that.

        Only the operations that *write or delete* a path raise. Reading goes
        through :func:`is_safe_name` instead and treats an unusable name as "no
        login", so a legal profile like ``prod/eu`` still runs against its own
        ``api_key``.
    """
    if not is_safe_name(profile):
        raise OvxError(f"refusing profile name {profile!r}: it must not contain a path")
    return token_dir / f"{profile}.json"


def claims(token: str) -> dict[str, object]:
    """Return a JWT's payload, or an empty dict when it cannot be read.

    The signature is deliberately not verified: that is the server's job, and
    ovx has no key to check it with. Only ``exp`` is used, and a token whose
    ``exp`` is wrong fails at the server anyway.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    # base64url without padding is what JWTs use; restore it before decoding.
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def expiry_of(token: str) -> float:
    """Return a token's ``exp`` claim.

    Raises
    ------
    OvxError
        If the token carries no usable ``exp``. Without one ovx cannot re-mint
        ahead of time, so the token would be used until the server rejected
        it — and an identity token with no expiry is not the credential this
        was built for.
    """
    expires_at = claims(token).get("exp")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise OvxError("the minted token carries no usable 'exp' claim")
    return float(expires_at)


def save(token_dir: Path, profile: str, token: str, role: str) -> StoredLogin:
    """Write a freshly minted token to the profile's token file.

    The expiry is read out of the JWT rather than assumed from the role's TTL:
    the TTL is server-side configuration that can change without ovx knowing,
    while ``exp`` is what the server will actually enforce.

    Parameters
    ----------
    token_dir :
        Directory holding one file per profile.
    profile :
        Profile the token belongs to.
    token :
        The signed JWT.
    role :
        Vault role it came from, recorded for the operator's benefit.

    Returns
    -------
    StoredLogin
        What was written.

    Raises
    ------
    OvxError
        If the token has no usable expiry, or the file cannot be written.
    """
    login = StoredLogin(
        profile=profile, role=role, token=token, expires_at=expiry_of(token)
    )
    path = token_path(token_dir, profile)
    record = {
        "profile": login.profile,
        "kind": "vault-identity",
        "role": login.role,
        "token": login.token,
        "expires_at": login.expires_at,
    }
    try:
        write_private(path, json.dumps(record, indent=2) + "\n")
    except OSError as error:
        raise OvxError(f"could not write {path}: {error}") from None
    return login


def load(token_dir: Path, profile: str) -> StoredLogin | None:
    """Return the stored login for a profile, or ``None`` if there is none.

    Parameters
    ----------
    token_dir :
        Directory holding one file per profile.
    profile :
        Profile name.

    Returns
    -------
    StoredLogin | None
        The stored login, or ``None`` when the profile has never logged in.

    Raises
    ------
    UnusableLogin
        If a login exists but cannot be used — unreadable, malformed, or
        written by a version of ovx that predates Vault identity tokens.
        This is deliberately not ``None``: the caller must abort rather than
        fall back to the profile's ``api_key``.
    """
    # A path-shaped name could never have had a login written under it, so this
    # is "no login" rather than an error. Raising here would brick a legal
    # profile -- ["prod/eu"] is valid TOML -- that has a working api_key and
    # never logged in.
    if not is_safe_name(profile):
        return None
    path = token_dir / f"{profile}.json"
    hint = f"Run 'ovx --login {profile}' to replace it, or delete {path}."

    # Deciding "is there a login here" with is_file() cannot tell absent from
    # unreadable. A token directory that lost its +x bit, a dangling symlink, a
    # directory, or a FIFO at the path all answer False -- and each would then
    # be read as "never logged in" and silently fall back to the api_key, which
    # is the downgrade this module exists to prevent. Stat it and classify, so
    # only a genuinely missing file is None.
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UnusableLogin(
            f"cannot reach the token file {path}: {error}", hint
        ) from None

    if stat.S_ISLNK(status.st_mode):
        try:
            status = path.stat()
        except OSError as error:
            raise UnusableLogin(
                f"the token file {path} is a symlink that does not resolve: {error}",
                hint,
            ) from None
    if not stat.S_ISREG(status.st_mode):
        # Never open it: reading a FIFO would block for ever.
        raise UnusableLogin(f"the token file {path} is not a regular file", hint)

    try:
        raw = path.read_text()
    except OSError as error:
        raise UnusableLogin(f"cannot read the token file {path}: {error}", hint) from None
    try:
        record = json.loads(raw)
    except ValueError as error:
        raise UnusableLogin(
            f"the token file {path} is not valid JSON: {error}", hint
        ) from None

    # A JSON document need not be an object, and a hand-edited or half-written
    # file can be any shape at all. Check rather than assume, so a malformed
    # record produces a message instead of a traceback.
    if not isinstance(record, dict):
        raise UnusableLogin(f"the token file {path} does not hold an object", hint)

    token = record.get("token")
    if token is None and record.get("access_token"):
        # An OAuth-era record. Its ovat_ token cannot be renewed, and
        # OpenViking's OAuth endpoints now answer 503.
        raise UnusableLogin("this stored login predates Vault identity tokens", hint)
    if not isinstance(token, str) or not token:
        raise UnusableLogin(f"the token file {path} carries no usable token", hint)

    expires_at = record.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise UnusableLogin(f"the token file {path} carries no usable expiry", hint)

    role = record.get("role")
    return StoredLogin(
        profile=profile,
        role=role if isinstance(role, str) else "",
        token=token,
        expires_at=float(expires_at),
    )


def forget(token_dir: Path, profile: str) -> bool:
    """Delete a profile's stored login.

    There is no server-side revoke for a Vault identity token: it is a signed
    assertion, not a session, and nothing tracks it. Revoking the *Vault*
    session would be a much bigger hammer, taking every other tool on the
    machine with it, so it is deliberately not done. A token that leaked
    before this call stays valid until it expires.

    Returns
    -------
    bool
        True when a login was removed, False when there was none.
    """
    path = token_path(token_dir, profile)
    if not path.is_file():
        return False
    path.unlink()
    return True
