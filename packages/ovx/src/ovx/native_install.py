"""Registering the native-messaging host with Firefox.

Firefox will only run a native host it has been told about, and only for the
extensions the manifest names. That manifest is a small JSON file in a
per-platform directory; writing it is the whole install step.

``allowed_extensions`` is the security boundary. Any extension not listed
cannot start this host, so a hostile add-on cannot ask ovx for your token.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from ovx.errors import OvxError

#: What the extension passes to `browser.runtime.sendNativeMessage`.
HOST_NAME = "ovx"

#: The one extension allowed to talk to the host, from ov-clip's manifest.
OV_CLIP_ID = "ov-clip@openviking"

#: The console script Firefox executes. A manifest cannot carry arguments, so
#: the host needs an entry point of its own rather than a flag on `ovx`.
HOST_COMMAND = "ovx-firefox-host"


def manifest_dir(platform: str | None = None, home: Path | None = None) -> Path:
    """Return the directory Firefox reads native-host manifests from.

    Parameters
    ----------
    platform :
        ``sys.platform`` value to resolve for. Defaults to the running one.
    home :
        Home directory. Defaults to the real one.

    Returns
    -------
    Path
        The per-user manifest directory for this platform.

    Raises
    ------
    OvxError
        On a platform where Firefox locates manifests somewhere this does not
        know about — Windows keeps them in the registry, not on disk, so
        pretending to install one there would be a lie.
    """
    base = Path.home() if home is None else home
    system = sys.platform if platform is None else platform

    if system == "darwin":
        return base / "Library/Application Support/Mozilla/NativeMessagingHosts"
    if system.startswith("linux") or system.startswith("freebsd"):
        return base / ".mozilla/native-messaging-hosts"
    raise OvxError(
        f"no known Firefox native-messaging directory for {system!r}",
        "On Windows the manifest is registered in the registry; install by hand.",
    )


def host_path() -> Path:
    """Return the absolute path to the host executable.

    Firefox executes this path directly, with no shell and no ``$PATH`` lookup,
    so a bare command name would not work and a relative one would resolve
    against whatever directory Firefox happens to be in.

    The interpreter's own ``bin`` is tried before ``$PATH``, because the
    manifest outlives the shell that wrote it. ``uv run ovx
    --install-firefox-host`` from a checkout puts an ephemeral project
    environment first on ``$PATH``; baking that in gives a manifest that works
    until the environment is rebuilt and then points at nothing — reported by
    Firefox as "no such native application", which is indistinguishable from
    never having installed it. Alongside *this* interpreter is where the ovx
    being asked to install actually lives.

    Raises
    ------
    OvxError
        When the console script cannot be found at all, which means ovx was
        installed in a way that did not create it.
    """
    beside = Path(sys.executable).resolve().parent / HOST_COMMAND
    if beside.is_file():
        return beside

    found = shutil.which(HOST_COMMAND)
    if not found:
        raise OvxError(
            f"{HOST_COMMAND} is not next to {sys.executable} and not on $PATH",
            "Reinstall ovx so its console scripts are created: `uv tool install ovx`.",
        )
    return Path(found).resolve()


def build_manifest(executable: Path, extension_ids: list[str]) -> dict[str, object]:
    """Return the manifest body Firefox expects."""
    return {
        "name": HOST_NAME,
        "description": "Hands ovx's Vault identity token to an allowed extension.",
        "path": str(executable),
        "type": "stdio",
        # Firefox keys on extension ids. Chrome uses `allowed_origins` instead,
        # so this manifest is Firefox's alone.
        "allowed_extensions": extension_ids,
    }


def install(
    *,
    extension_ids: list[str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
    executable: Path | None = None,
) -> Path:
    """Write the manifest, and return where it went.

    Parameters
    ----------
    extension_ids :
        Extensions allowed to start the host. Defaults to ov-clip alone.
    platform :
        Override for testing.
    home :
        Override for testing.
    executable :
        Override for testing. Resolved from ``$PATH`` when omitted.

    Returns
    -------
    Path
        The manifest that was written.

    Raises
    ------
    OvxError
        If the platform is unsupported, the host is not on ``$PATH``, or the
        file cannot be written.
    """
    directory = manifest_dir(platform, home)
    target = directory / f"{HOST_NAME}.json"
    body = build_manifest(
        executable if executable is not None else host_path(),
        extension_ids if extension_ids is not None else [OV_CLIP_ID],
    )

    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Not via write_private: this holds no secret, and 0600 on a file
        # Firefox reads as your own user is right either way. 0644 matches what
        # every other native host ships and avoids looking anomalous.
        target.write_text(json.dumps(body, indent=2) + "\n")
        os.chmod(target, 0o644)
    except OSError as error:
        raise OvxError(f"could not write {target}: {error}") from None
    return target


def uninstall(*, platform: str | None = None, home: Path | None = None) -> bool:
    """Remove the manifest.

    Returns
    -------
    bool
        True when a manifest was removed, False when there was none.
    """
    target = manifest_dir(platform, home) / f"{HOST_NAME}.json"
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise OvxError(f"could not remove {target}: {error}") from None
    return True


__all__ = [
    "HOST_COMMAND",
    "HOST_NAME",
    "OV_CLIP_ID",
    "build_manifest",
    "host_path",
    "install",
    "manifest_dir",
    "uninstall",
]
