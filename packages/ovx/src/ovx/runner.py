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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from ovx.config import Profile, banner, materialize
from ovx.errors import OvxError
from ovx.fs import PRIVATE_DIR

# How long ov is given to shut down after a signal before ovx stops waiting
# and cleans up regardless. The credential comes off disk either way.
OV_SHUTDOWN_GRACE_SECONDS = 5.0

# Signals worth cleaning up after. SIGKILL cannot be caught and does leave the
# file behind, as it must.
_TRAPPED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class _SignalBox:
    """Records the first trapped signal, without interrupting anything.

    Attributes
    ----------
    signum : int | None
        The signal received, or ``None``.
    """

    def __init__(self) -> None:
        self.signum: int | None = None


@contextmanager
def _cleanup_on_signal() -> Iterator[_SignalBox]:
    """Record SIGINT, SIGTERM and SIGHUP instead of acting on them.

    Python's default handlers for TERM and HUP end the interpreter outright,
    skipping every ``finally`` and leaving the materialized credential on disk.
    SIGINT raises ``KeyboardInterrupt``, which unwinds from wherever it lands.

    Both are wrong here. Raising from the handler means a signal arriving
    during ``mkdtemp``, ``materialize``, ``Popen`` or -- worst -- during the
    ``rmtree`` that removes the credential, escapes as an exception: the first
    three surface a traceback, and the last aborts the removal and leaves a
    live token on disk for good. A second Ctrl-C is enough to do it.

    So the handler only records. Nothing is interrupted, every ``finally``
    runs to completion, and the caller re-raises the signal afterwards, once
    the temp directory is gone.

    ``ov`` shares this terminal's process group, so it receives the signal
    directly and shuts itself down; ovx simply waits for it, which is what the
    shell version did.
    """
    box = _SignalBox()

    def handle(signum: int, _frame: FrameType | None) -> None:
        if box.signum is None:
            box.signum = signum

    previous = {}
    for signum in _TRAPPED:
        try:
            previous[signum] = signal.signal(signum, handle)
        except (ValueError, OSError):
            # Not the main thread, or the platform lacks it. Cleanup still
            # happens through `finally`; only the signal path is unprotected.
            pass
    try:
        yield box
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

    with _cleanup_on_signal() as signals, private_workspace() as root:
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
        code = _wait_for_ov(args, env, signals)

    # Outside the `with`, so the temp directory is already gone. Killing
    # ourselves inside it would end the process at the os.kill syscall and the
    # cleanup would never run, leaving the materialized credential on disk for
    # good -- which is the one thing this tool exists to prevent.
    if signals.signum is not None:
        _die_from(signals.signum)
    return code


def _wait_for_ov(args: list[str], env: dict[str, str], signals: _SignalBox) -> int:
    """Run ``ov`` to completion and return its exit code.

    ``subprocess.run`` is not used: on an exception it kills the child, and
    ``ov`` may be shutting down cleanly.

    When a signal arrives it is forwarded to ``ov`` and then waited on for a
    short grace period. Waiting indefinitely would hang ovx behind an ``ov``
    that ignores the signal; not waiting at all would kill it mid-shutdown.
    Either way the temp directory is removed afterwards, because the caller
    does that on the way out of the ``with``.

    Parameters
    ----------
    args :
        Arguments forwarded to ``ov``.
    env :
        Environment for the child.
    signals :
        Where the handler records a trapped signal.

    Returns
    -------
    int
        ``ov``'s exit code, or ``128 + signum`` if it outlived the grace
        period.
    """
    process = subprocess.Popen(["ov", *args], env=env)
    forwarded = False
    deadline = 0.0
    while True:
        try:
            return process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        if signals.signum is None:
            continue
        if not forwarded:
            forwarded = True
            deadline = time.monotonic() + OV_SHUTDOWN_GRACE_SECONDS
            # ov usually has the signal already -- it shares this terminal's
            # process group -- but not when ovx alone was signalled.
            try:
                process.send_signal(signals.signum)
            except OSError:
                pass
        elif time.monotonic() > deadline:
            return 128 + signals.signum


def _die_from(signum: int) -> None:
    """Die from ``signum`` with the default handler, after cleanup has run.

    Exiting with a plain code would hide the interrupt from the calling shell,
    which tells "the user pressed Ctrl-C" from "the command failed" by the
    signal. Only call this once the temp directory is removed.
    """
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
