"""Writing secrets to disk without a window where they are readable."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


def write_private(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` at mode 0600, replacing atomically.

    The mode is set on the descriptor before anything is written, so the file
    is never briefly world-readable — a ``chmod`` afterwards leaves exactly
    that window. The temporary file is named by ``mkstemp`` rather than a fixed
    ``<path>.new``: two ovx runs re-minting at once would share that name, and
    one could replace the real file with the other's half-written copy.

    Parameters
    ----------
    path :
        Destination. Its parent directory is created if missing.
    content :
        Text to write.

    Raises
    ------
    OSError
        If the directory cannot be created, or the write or replace fails.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ovx-tmp.")
    try:
        os.fchmod(fd, PRIVATE_FILE)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except OSError:
        # Leaving the temp file behind would litter the directory with
        # partial credentials.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` if missing and make it owner-only.

    A failure to tighten the mode is not fatal: the directory may live on a
    filesystem that does not carry permissions, and ovx should still run.
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(PRIVATE_DIR)
    except OSError:
        pass
