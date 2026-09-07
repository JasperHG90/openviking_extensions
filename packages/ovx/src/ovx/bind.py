"""Binding a profile to ``ov``'s own config file.

This is the deliberate exception to everything else ovx does. The temp-file
mechanism means the credential exists only while one command runs, which is
the point — but it also means nothing else can see it. An agent, an editor
plugin, or a bare ``ov`` invocation has no way to reach a profile, because by
the time it looks the file is gone.

``bind`` trades that away on purpose: it writes the materialized profile to
``ovcli.conf`` and leaves it there. Everything ovx exists to avoid is then
true again — the credential is on disk indefinitely, lands in backups, and
shows up in anything that walks your home directory. So it says so, records
what it did, and ``unbind`` puts it back.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ovx.config import Profile, expand
from ovx.errors import OvxError
from ovx.fs import write_private
from ovx.paths import Locations

DEFAULT_OV_CONFIG = "~/.openviking/ovcli.conf"


def ov_config_file(environ: dict[str, str] | None = None) -> Path:
    """Return the config file ``ov`` will actually read.

    ``$OPENVIKING_CLI_CONFIG_FILE`` wins, which is the same variable ovx sets
    when it runs ``ov`` against a temp file — so binding writes where ov looks
    either way.
    """
    env = os.environ if environ is None else environ
    return Path(env.get("OPENVIKING_CLI_CONFIG_FILE") or DEFAULT_OV_CONFIG).expanduser()


@dataclass(frozen=True)
class Binding:
    """What ovx wrote, and where.

    Recorded in ovx's own directory rather than inside ``ovcli.conf``: ov's
    reader rejects unknown fields, so a marker in the file itself would break
    the very thing being bound.

    Attributes
    ----------
    profile : str
        Profile that was bound.
    target : Path
        The ``ovcli.conf`` written.
    expires_at : float
        Unix time the bound credential stops working, or ``0`` when it is a
        static key that does not expire.
    """

    profile: str
    target: Path
    expires_at: float


def _record_path(locations: Locations) -> Path:
    """Return the file recording the current binding."""
    return locations.root / "bound.json"


def read_binding(locations: Locations) -> Binding | None:
    """Return the binding ovx last wrote, or ``None``."""
    try:
        record = json.loads(_record_path(locations).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or "target" not in record:
        return None
    return Binding(
        profile=str(record.get("profile", "")),
        target=Path(str(record["target"])),
        expires_at=float(record.get("expires_at") or 0),
    )


def bind(
    locations: Locations,
    profile: Profile,
    *,
    token: str = "",
    expires_at: float = 0.0,
    environ: dict[str, str] | None = None,
) -> Binding:
    """Write ``profile`` to ov's config file and record that ovx did it.

    Parameters
    ----------
    locations :
        Where ovx keeps its own state.
    profile :
        The profile to bind.
    token :
        A stored login, which outranks the profile's ``api_key`` exactly as it
        does for a normal run.
    expires_at :
        When that token stops working, for the reminder printed afterwards.
    environ :
        Environment to expand ``$VAR`` from. Defaults to the real one.

    Returns
    -------
    Binding
        What was written.

    Raises
    ------
    OvxError
        If the file cannot be written.
    """
    target = ov_config_file(environ)
    settings = expand(profile, environ)
    if token:
        settings["api_key"] = token

    try:
        write_private(target, json.dumps(settings, indent=2) + "\n")
    except OSError as error:
        raise OvxError(f"could not write {target}: {error}") from None

    binding = Binding(profile=profile.name, target=target, expires_at=expires_at)
    try:
        write_private(
            _record_path(locations),
            json.dumps(
                {
                    "profile": binding.profile,
                    "target": str(binding.target),
                    "expires_at": binding.expires_at,
                },
                indent=2,
            )
            + "\n",
        )
    except OSError:
        # The credential is written; failing to record it only costs `unbind`
        # its safety check, which it reports for itself.
        pass
    return binding


def unbind(locations: Locations, *, environ: dict[str, str] | None = None) -> Path:
    """Remove the config ovx bound, and forget the binding.

    Refuses to delete a file ovx did not write. The target is ov's ordinary
    config path, which an operator may well have set up by hand long before
    ovx existed; removing that on their behalf would be an unpleasant
    surprise.

    Returns
    -------
    Path
        The file that was removed.

    Raises
    ------
    OvxError
        If nothing is bound, or the file on disk is not the one ovx wrote.
    """
    binding = read_binding(locations)
    if binding is None:
        raise OvxError(
            "nothing is bound",
            f"ovx only removes a config it wrote; {ov_config_file(environ)} was not one.",
        )
    if not binding.target.is_file():
        _record_path(locations).unlink(missing_ok=True)
        raise OvxError(f"{binding.target} is already gone")

    binding.target.unlink()
    _record_path(locations).unlink(missing_ok=True)
    return binding.target
