"""Reading, validating and materializing profiles.

A profile is one TOML table naming an OpenViking instance. Its keys are the
keys of ``ovcli.conf``, so materializing a profile is mostly validation plus
``$VAR`` expansion — the point being that the secret lives in the environment
and only ever reaches a private temporary file.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ovx.errors import OvxError

# Every field ovcli.conf accepts, with the JSON type ovx writes for it.
#
# The list mirrors OVCLIConfig in openviking_cli/utils/config/ovcli_config.py.
# That reader sets extra="forbid", so a field absent here is one it would
# reject anyway; the Rust CLI is laxer and ignores what it does not know, which
# is worse — a typo'd key silently does nothing. Checking against this list
# turns both cases into one error naming the offending key.
FIELD_TYPES: dict[str, str] = {
    "url": "str",
    "api_key": "str",
    "root_api_key": "str",
    "account": "str",
    "user": "str",
    "actor_peer_id": "str",
    "agent_id": "str",
    "timeout": "num",
    "profile": "bool",
    "output": "str",
    "echo_command": "bool",
    "show_progress": "bool",
    "verbose": "bool",
    "upload": "map",
    "extra_headers": "map",
    "extra_header": "map",
    "gateway_token": "str",
    "plugin": "map",
    "auth_mode": "str",
    "ldap_username": "str",
    "ldap_password": "str",
}

# The fields the wizard prompts for, in order. A profile may carry any field
# from FIELD_TYPES — the rest are for hand-editing, and an edit leaves them
# untouched rather than dropping what it cannot prompt for.
WIZARD_FIELDS = ("url", "api_key", "account", "user")

_TYPE_CHECKS: dict[str, tuple[type | tuple[type, ...], str]] = {
    "str": (str, "a string"),
    "num": ((int, float), "a number"),
    "bool": (bool, "true or false"),
    "map": (dict, "a table"),
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Profile:
    """One validated profile, before ``$VAR`` expansion.

    Attributes
    ----------
    name : str
        The TOML table name.
    fields : dict[str, object]
        Its keys and values, checked against ``FIELD_TYPES``.
    """

    name: str
    fields: dict[str, object]

    @property
    def url(self) -> str:
        """The instance URL, unexpanded."""
        return str(self.fields.get("url", ""))

    @property
    def user(self) -> str:
        """The ``user`` field, unexpanded, or an empty string."""
        return str(self.fields.get("user", "")).strip()


def load_document(path: Path) -> dict[str, object]:
    """Parse the ovx config file.

    Returns
    -------
    dict[str, object]
        The parsed TOML, empty when the file does not exist.

    Raises
    ------
    OvxError
        If the file exists but cannot be read or parsed.
    """
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise OvxError(f"failed to parse {path}: {error}", code=2) from None
    except OSError as error:
        raise OvxError(f"cannot read {path}: {error}") from None


def profile_names(path: Path) -> list[str]:
    """Return every profile name in the config, sorted."""
    document = load_document(path)
    return sorted(k for k, v in document.items() if isinstance(v, dict))


def load_profile(path: Path, name: str) -> Profile:
    """Load one profile and check it.

    Every field is checked against the type ``ovcli.conf`` expects, because a
    TOML value of the wrong type is rejected by ``ov`` after ovx has already
    reported success.

    Raises
    ------
    OvxError
        If the profile is missing, is not a table, carries an unknown field,
        has no ``url``, or holds a value of the wrong type.
    """
    document = load_document(path)
    if not document:
        raise OvxError(f"config file not found: {path}")
    if name not in document:
        available = ", ".join(profile_names(path)) or "(none)"
        raise OvxError(f"profile {name!r} not found. Available: {available}")

    section = document[name]
    if not isinstance(section, dict):
        raise OvxError(f"profile {name!r} is not a table", code=2)

    for key in section:
        if key not in FIELD_TYPES:
            allowed = ", ".join(sorted(FIELD_TYPES))
            raise OvxError(
                f"profile {name!r} has unknown field {key!r}",
                f"allowed: {allowed}",
                code=2,
            )

    if not str(section.get("url", "")).strip():
        raise OvxError(f"profile {name!r} is missing required field 'url'", code=2)

    for key, value in section.items():
        want, describe = _TYPE_CHECKS[FIELD_TYPES[key]]
        # bool is a subclass of int, so a bare `true` would satisfy a num check.
        ok = not isinstance(value, bool) if FIELD_TYPES[key] == "num" else True
        if not (ok and isinstance(value, want)):
            raise OvxError(f"profile {name!r} field {key!r} must be {describe}", code=2)

    return Profile(name=name, fields=dict(section))


def expand(profile: Profile, environ: dict[str, str] | None = None) -> dict[str, object]:
    """Expand ``$VAR`` and ``${VAR}`` through a profile's string values.

    Nested tables are walked, so a variable inside ``extra_headers`` expands
    too.

    Raises
    ------
    OvxError
        If a referenced variable is unset or empty. A missing secret fails
        loudly rather than running against a half-built config.
    """
    env = os.environ if environ is None else environ

    def replace(value: object, key: str) -> object:
        if isinstance(value, str):

            def one(match: re.Match[str]) -> str:
                var = match.group(1) or match.group(2)
                if not env.get(var):
                    raise OvxError(
                        f"profile {profile.name!r} field {key!r} references "
                        f"${var} which is not set",
                        code=4,
                    )
                return env[var]

            return _VAR.sub(one, value)
        if isinstance(value, dict):
            return {k: replace(v, f"{key}.{k}") for k, v in value.items()}
        return value

    return {k: replace(v, k) for k, v in profile.fields.items()}


def materialize(
    profile: Profile,
    dest: Path,
    *,
    token: str = "",
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    """Write a profile to ``dest`` as an ``ovcli.conf``.

    Parameters
    ----------
    profile :
        The profile to write.
    dest :
        Path to create. It must not already exist.
    token :
        A stored login, which outranks whatever the profile says.

        ``ov`` has no separate field for a bearer token and needs none: the
        server's ``_extract_token`` treats an ``api_key`` with exactly two dots
        as a JWT and uses it as one. A Vault identity token has two dots, so it
        travels in the ``api_key`` slot untouched. ``ov`` also copies anything
        with two or more dots into an ``Authorization: Bearer`` header; both
        carry the same token and the server reads either, so the duplication is
        harmless and deliberately left alone.
    environ :
        Environment to expand from. Defaults to the real one.

    Returns
    -------
    dict[str, object]
        What was written.

    Raises
    ------
    OvxError
        If expansion fails or the file cannot be created.
    """
    out = expand(profile, environ)
    if token:
        out["api_key"] = token

    # O_EXCL so a pre-existing path is never followed or overwritten, and 0600
    # from the start rather than a chmod after the secret has already landed.
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(out, handle, indent=2)
            handle.write("\n")
    except OSError as error:
        raise OvxError(f"could not write {dest}: {error}") from None
    return out


def banner(name: str, config: dict[str, object]) -> str:
    """Return the one-line summary of what a command is about to hit.

    Naming a profile is the whole point of ovx, so each run says which
    instance it resolved to. The key is masked: this goes to a terminal, and a
    credential on screen outlives the command in scrollback.
    """
    key = str(config.get("api_key") or "")
    shown = f"{key[:4]}…" if len(key) > 8 else ("<set>" if key else "<unset>")
    return f"ovx: profile={name}  url={config.get('url', '<unset>')}  api_key={shown}"
