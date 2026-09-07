"""Turning a stored login into a token something can actually use.

Shared by the CLI and the Firefox native-messaging host. Both answer the same
question — "give me a live token for this profile" — and both must answer it
identically: a second implementation would be free to drift into falling back
to the profile's ``api_key`` where this one refuses, which is the downgrade
:mod:`ovx.tokens` exists to prevent.
"""

from __future__ import annotations

from ovx import tokens, vault
from ovx.errors import OvxError, UnusableLogin
from ovx.paths import Locations


def current_token(locations: Locations, profile: str) -> tuple[str, bool]:
    """Return a usable token for a profile, minting a replacement if stale.

    Renewal is meant to be invisible while the Vault session lives; it only
    speaks up when it cannot be done. Nothing here prompts, so it is safe to
    call with no terminal attached.

    Parameters
    ----------
    locations :
        Where the config and tokens live.
    profile :
        Profile to read the login for.

    Returns
    -------
    tuple[str, bool]
        The token (empty when the profile has never logged in), and whether a
        login existed at all. The second value matters: no login is ordinary
        and falls back to the profile's ``api_key``, while a *broken* login
        raises instead of downgrading.

    Raises
    ------
    UnusableLogin
        When a login exists but cannot be renewed, carrying what actually
        failed rather than a blanket "your session is gone".
    """
    stored = tokens.load(locations.token_dir, profile)
    if stored is None:
        return "", False
    if not stored.is_stale():
        return stored.token, True

    if not vault.address():
        raise UnusableLogin(
            f"the stored login for {profile!r} expired and $VAULT_ADDR is not set",
            "Set it to renew, or set the profile's api_key.",
        )
    try:
        vault.check_session()
    except vault.VaultError as error:
        # Say what actually failed. "Your session is gone" sent the operator to
        # `ovx --login`, which fails identically when the real cause was a bad
        # $VAULT_CACERT or a Vault that is simply unreachable.
        raise UnusableLogin(
            f"the stored login for {profile!r} expired and cannot be renewed: {error}",
            f"If your Vault session lapsed, run 'ovx --login {profile}'.",
        ) from None

    # $OVX_VAULT_ROLE wins over the role recorded in the token file. The role
    # fixes the token's audience and its ov_account claim, so renewing from a
    # stale recorded role silently re-authenticates as a different identity --
    # and a renewal is meant to be invisible, so nothing would say so.
    chosen = vault.role()
    try:
        token = vault.mint(chosen)
        tokens.save(locations.token_dir, profile, token, chosen)
    except OvxError as error:
        # Still a login that exists and cannot be used, so it must exit 2 and
        # never fall back to the profile's api_key.
        raise UnusableLogin(
            f"the stored login for {profile!r} expired and could not be renewed: {error}",
            f"Run 'ovx --login {profile}' and try again.",
        ) from None
    return token, True


__all__ = ["current_token"]
