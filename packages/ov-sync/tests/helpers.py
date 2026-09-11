"""Builders shared by the test modules."""

from __future__ import annotations

from pathlib import Path


def write(path: Path, content: str | bytes) -> Path:
    """Create a file, and any parent directories it needs.

    Parameters
    ----------
    path :
        File to write.
    content :
        Text or bytes to put in it.

    Returns
    -------
    Path
        The file that was written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path
