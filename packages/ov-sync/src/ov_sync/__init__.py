"""Sync a local folder into OpenViking, one way, with local state.

A folder maps onto a ``viking://`` directory positionally: a file's path under
the folder is its path under the root. That mapping is the whole idempotency
story — the target URI is the key, so re-sending a file overwrites it rather
than duplicating it, and a run can be repeated or interrupted freely.

What makes a second run cheap is the state file, one SQLite database in the
folder's root recording what was sent and what it hashed to. A run compares
size and mtime first, hashes only what moved, and sends only what the hash says
actually changed — an editor that rewrites a file byte-for-byte costs nothing,
where a blind re-upload would cost a re-embed per file.

Writes go through OpenViking's ``content/batch-write`` endpoint, up to 256
files per request, because that endpoint reindexes once for the whole batch.

``ov`` itself has no sync command; this fills that gap and nothing else. It
does not pull, and it does not merge: OpenViking is the copy, the folder is the
original.
"""

from __future__ import annotations

from .client import OvClient, OvError
from .config import OvSyncConfig, load_config, load_credentials
from .engine import SyncPlan, SyncResult, plan_sync, sync
from .state import RootUriMismatch, SyncState

__all__ = [
    "OvClient",
    "OvError",
    "OvSyncConfig",
    "RootUriMismatch",
    "SyncPlan",
    "SyncResult",
    "SyncState",
    "__version__",
    "load_config",
    "load_credentials",
    "plan_sync",
    "sync",
]

try:  # populated by hatch-vcs at build time
    from ._version import __version__
except ImportError:  # editable install or source checkout
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("ov-sync")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"
