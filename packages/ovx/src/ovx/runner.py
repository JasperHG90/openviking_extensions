"""Running ``ov`` against a materialized profile.

The whole point of ovx is that the credential exists on disk only for as long
as the command runs. That makes cleanup the load-bearing part of this module,
including when the operator interrupts.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from ovx.config import Profile, banner, materialize
from ovx.errors import OvxError
from ovx.fs import PRIVATE_DIR

# Signals worth cleaning up after. SIGKILL cannot be caught and does leave the
# file behind, as it must.
_TRAPPED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class _Interrupted(Exception):
    """A trapped signal arrived; unwind so the temp directory is removed."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@contextmanager
def _cleanup_on_signal() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into exceptions so ``finally`` blocks run.

    Python's default handlers for these terminate the interpreter outright,
    which skips every ``finally`` and would leave the materialized credential
    on disk. SIGINT already raises ``KeyboardInterrupt``, so it needs no help,
    but it is trapped alongside them to keep the re-raise uniform.

    Each signal is re-raised with the default handler after cleanup, so ovx
    still dies from it and a calling script can tell an interrupt from a
    failure.
    """

    def handle(signum: int, _frame: FrameType | None) -> None:
        raise _Interrupted(signum)

    previous = {}
    for signum in _TRAPPED:
        try:
            previous[signum] = signal.signal(signum, handle)
        except (ValueError, OSError):
            # Not the main thread, or the platform lacks it. Cleanup still
            # happens through `finally`; only the signal path is unprotected.
            pass
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@contextmanager
def private_workspace() -> Iterator[Path]:
    """Yield a private temporary directory, removed on the way out.

    Removed even when ov fails, and when the operator interrupts. ``rm``
    unlinks rather than erases: on an SSD the blocks may persist until reused,
    so this bounds exposure rather than eliminating it.
    """
    root = Path(tempfile.mkdtemp(prefix="ovx."))
    try:
        root.chmod(PRIVATE_DIR)
    except OSError:
        pass
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_ov(
    profile: Profile,
    args: list[str],
    *,
    token: str = "",
    warn_no_credential: bool = False,
) -> int:
    """Materialize ``profile`` and run ``ov`` against it.

    ``ov`` runs as a child rather than through ``exec`` precisely so this
    process survives to delete the config.

    Parameters
    ----------
    profile :
        The profile to run under.
    args :
        Arguments forwarded to ``ov`` untouched.
    token :
        A stored login, which outranks the profile's own ``api_key``.
    warn_no_credential :
        Say how to log in when the profile ends up with no credential at all.
        ``ov``'s own 401 does not mention it.

    Returns
    -------
    int
        ``ov``'s exit code.

    Raises
    ------
    OvxError
        If ``ov`` is not installed, or the config cannot be written.
    """
    if shutil.which("ov") is None:
        raise OvxError("required command not found: ov")

    with _cleanup_on_signal(), private_workspace() as root:
        # Named ovcli.conf, not a random name: ov derives sibling paths such
        # as ovcli.conf.<name> from it.
        conf = root / "ovcli.conf"
        written = materialize(profile, conf, token=token)

        if warn_no_credential and not written.get("api_key"):
            print(
                f"ovx: no stored login for {profile.name!r}, and the profile "
                "has no api_key.",
                file=sys.stderr,
            )
            print(
                f"     Run 'ovx --login {profile.name}' if this server wants one.",
                file=sys.stderr,
            )

        # The banner confirms which instance the command is about to hit,
        # which is the whole point of naming a profile. Only on a terminal: in
        # a pipeline it would corrupt whatever is reading ov's stderr.
        if sys.stderr.isatty():
            print(banner(profile.name, written), file=sys.stderr)

        env = dict(os.environ)
        env["OPENVIKING_CLI_CONFIG_FILE"] = str(conf)
        code, interrupted = _wait_for_ov(args, env)

    # Outside the `with`, so the temp directory is already gone. Killing
    # ourselves inside it would end the process at the os.kill syscall and the
    # cleanup would never run, leaving the materialized credential on disk for
    # good -- which is the one thing this tool exists to prevent.
    if interrupted is not None:
        _die_from(interrupted)
    return code


def _wait_for_ov(args: list[str], env: dict[str, str]) -> tuple[int, int | None]:
    """Run ``ov`` to completion, tolerating a signal along the way.

    ``subprocess.run`` is not used here: on an exception it kills the child.
    ``ov`` shares this terminal's process group, so it receives Ctrl-C
    directly and may be shutting down cleanly — killing it would cut that
    short. Instead the wait is resumed until the child actually exits, which
    is what the shell version did.

    Parameters
    ----------
    args :
        Arguments forwarded to ``ov``.
    env :
        Environment for the child.

    Returns
    -------
    tuple[int, int | None]
        ``ov``'s exit code, and the signal to die from afterwards, if any.
    """
    process = subprocess.Popen(["ov", *args], env=env)
    interrupted: int | None = None
    while True:
        try:
            return process.wait(), interrupted
        except _Interrupted as signalled:
            # Remember it and keep waiting; ov is handling the same signal.
            interrupted = interrupted or signalled.signum
        except KeyboardInterrupt:
            interrupted = interrupted or signal.SIGINT


def _die_from(signum: int) -> None:
    """Die from ``signum`` with the default handler, after cleanup has run.

    Exiting with a plain code would hide the interrupt from the calling shell,
    which tells "the user pressed Ctrl-C" from "the command failed" by the
    signal. Only call this once the temp directory is removed.
    """
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
