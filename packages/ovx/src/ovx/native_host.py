"""Answering a browser extension that needs the token ovx already holds.

A Firefox extension has no filesystem access — there is no read API, and unlike
Chrome it cannot be granted host permissions for ``file://`` — so it cannot
open ``~/.ovx/tokens/<profile>.json`` itself. Native messaging is the
documented way across: Firefox runs this program, hands it one message on
stdin, and reads one reply from stdout.

What that buys is worth stating, because it is the whole point of the design:
the extension never sees a Vault password, never talks to Vault, and stores no
credential of its own. It asks here per save, and ovx answers with a token it
renews from the live Vault session exactly as it does for ``ov``.

The wire format is Firefox's: a four-byte native-endian length, then that many
bytes of UTF-8 JSON. Both directions.
"""

from __future__ import annotations

import io
import json
import struct
import sys
from typing import IO

from ovx import __version__
from ovx.config import Profile, expand, load_profile, profile_names
from ovx.errors import OvxError
from ovx.login import current_token
from ovx.paths import Locations

# Firefox caps a message at 1 MB in each direction. Reading a declared length
# larger than that means the stream is not a browser talking to us, so refuse
# it rather than allocate whatever was asked for.
MAX_MESSAGE_BYTES = 1024 * 1024

# Fields of a profile the extension is allowed to learn. Just the address:
# OpenViking takes identity from the token's claims, not from `account` or
# `user`, so sending those would be payload nobody reads crossing a credential
# boundary. `api_key` is absent for a stronger reason — the extension gets the
# minted identity token or nothing, never a profile's static key, which is the
# credential ovx exists to stop leaving lying around.
PROFILE_FIELDS = ("url",)


def read_message(stream: IO[bytes]) -> dict[str, object] | None:
    """Read one length-prefixed message.

    Parameters
    ----------
    stream :
        Binary stream, normally stdin.

    Returns
    -------
    dict[str, object] | None
        The decoded message, or ``None`` at end of stream, which is how the
        browser says it is done.

    Raises
    ------
    OvxError
        If the length is implausible or the payload is not a JSON object.
    """
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("@I", header)
    if length > MAX_MESSAGE_BYTES:
        raise OvxError(f"message of {length} bytes is larger than the 1 MB cap")

    body = stream.read(length)
    if len(body) < length:
        raise OvxError("message ended early")
    try:
        decoded = json.loads(body)
    except ValueError as error:
        raise OvxError(f"message is not JSON: {error}") from None
    if not isinstance(decoded, dict):
        raise OvxError("message is not a JSON object")
    return decoded


def write_message(stream: IO[bytes], payload: dict[str, object]) -> None:
    """Write one length-prefixed message and flush it.

    Raises
    ------
    OvxError
        If the reply exceeds the cap. Firefox applies it in both directions,
        and a reply it drops looks to the extension like a host that answered
        nothing.
    """
    body = json.dumps(payload).encode()
    if len(body) > MAX_MESSAGE_BYTES:
        raise OvxError(f"reply of {len(body)} bytes is larger than the 1 MB cap")
    stream.write(struct.pack("@I", len(body)))
    stream.write(body)
    stream.flush()


def _profiles(locations: Locations) -> dict[str, object]:
    """Answer with the profile names, for the extension's picker."""
    return {"ok": True, "profiles": profile_names(locations.config_file)}


def _token(locations: Locations, message: dict[str, object]) -> dict[str, object]:
    """Answer with a live token for one profile, and where to spend it.

    The profile's ``url`` travels with the token so the extension has one thing
    to configure rather than two that can disagree.
    """
    name = message.get("profile")
    if not isinstance(name, str) or not name:
        raise OvxError("no profile named in the request")

    known = profile_names(locations.config_file)
    if name not in known:
        raise OvxError(
            f"profile {name!r} not found",
            f"known profiles: {', '.join(known) or '(none)'}",
        )

    token, had_login = current_token(locations, name)
    if not had_login:
        # Deliberately not falling back to the profile's api_key. The extension
        # asked for the login, and a static key is a different, longer-lived
        # credential the operator did not agree to hand a browser.
        raise OvxError(
            f"profile {name!r} has no stored login",
            f"Run 'ovx --login {name}' first.",
        )

    profile = load_profile(locations.config_file, name)
    return {"ok": True, "token": token, "profile": name, **_expanded(profile)}


def _expanded(profile: Profile) -> dict[str, object]:
    """Return the shareable profile fields, with ``$VAR`` resolved.

    A profile may write ``url = "$OV_URL"``, which is the whole point of ovx —
    the secret lives in the environment. Handing that string over unexpanded
    would have the extension fetch from a URL called ``$OV_URL``.

    Expansion reads *this* process's environment, and this process is started
    by Firefox, which does not inherit a shell's exports. So a reference that
    cannot be resolved is not an error: the token is still good, and the
    extension has its own address field to fall back on. The field is left out
    rather than sent wrong.
    """
    try:
        resolved = expand(profile)
    except OvxError:
        return {}
    return {
        key: str(resolved[key])
        for key in PROFILE_FIELDS
        if key in resolved and str(resolved[key]).strip()
    }


def handle(locations: Locations, message: dict[str, object]) -> dict[str, object]:
    """Work out the reply to one message.

    Every failure comes back as ``{"ok": false, ...}`` rather than as an exit
    code: the extension has no way to see a status, and a host that dies
    silently is reported to the user as "native application unavailable",
    which says nothing about what to fix.

    Parameters
    ----------
    locations :
        Where the config and tokens live.
    message :
        The decoded request.

    Returns
    -------
    dict[str, object]
        The reply to send back.
    """
    action = message.get("action")
    try:
        if action == "ping":
            return {"ok": True, "version": __version__}
        if action == "profiles":
            return _profiles(locations)
        if action == "token":
            return _token(locations, message)
    except OvxError as error:
        return {"ok": False, "error": str(error), "hint": error.hint}
    except Exception as error:
        # Anything unexpected still has to come back as a message. A traceback
        # on stderr is invisible to the extension, and an exit here reads as
        # "ovx is not installed".
        return {"ok": False, "error": f"ovx failed: {error}", "hint": ""}

    return {
        "ok": False,
        "error": f"unknown action {action!r}",
        "hint": "expected one of: ping, profiles, token",
    }


def main() -> int:
    """Serve one exchange on stdin and stdout.

    Firefox's ``sendNativeMessage`` starts this program, sends one message, and
    reads one reply, so a single round trip is the whole job. Arguments are
    ignored: a native-messaging manifest cannot carry any, and what Firefox
    passes is the manifest path and the calling extension's id.

    Returns
    -------
    int
        Process exit status.
    """
    stdin = sys.stdin.buffer
    # Nothing but protocol may reach stdout. A stray print anywhere below this
    # would be read as a message length and break the exchange, so the real
    # stdout is taken away and diagnostics are pointed at stderr.
    #
    # `detach()` rather than `.buffer`: it hands the raw stream over and gives
    # up ownership. Reading `.buffer` alone leaves the TextIOWrapper owning it,
    # and reassigning sys.stdout below drops the last reference to that
    # wrapper — whose finalizer then closes the very stream the reply goes to.
    #
    # Only a TextIOWrapper has detach; a stdout something else has replaced
    # falls back to its buffer, which is the best that can be done there.
    out = sys.stdout
    stdout: IO[bytes] = out.detach() if isinstance(out, io.TextIOWrapper) else out.buffer
    sys.stdout = sys.stderr

    # Everything below is inside one guard, and it catches Exception rather
    # than OvxError. `handle` has its own catch-all, but two things run outside
    # it: reading the message, where a deeply nested body raises RecursionError
    # from json, and resolving the locations. Either escaping would make the
    # process die with a traceback on stderr and nothing on stdout — which is
    # the silent death this whole design exists to avoid, reported to the
    # person as "native application unavailable".
    try:
        message = read_message(stdin)
        if message is None:
            return 0
        reply = handle(Locations.resolve(), message)
    except OvxError as error:
        write_message(stdout, {"ok": False, "error": str(error), "hint": error.hint})
        return 1
    except Exception as error:
        write_message(stdout, {"ok": False, "error": f"ovx failed: {error}", "hint": ""})
        return 1

    write_message(stdout, reply)
    return 0


def entrypoint() -> None:
    """Console-script entry point for ``ovx-firefox-host``."""
    raise SystemExit(main())


__all__ = ["entrypoint", "handle", "main", "read_message", "write_message"]
