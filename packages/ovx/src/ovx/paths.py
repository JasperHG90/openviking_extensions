"""Where ovx keeps its config and its stored logins."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Locations:
    """The files and directories ovx reads and writes.

    Attributes
    ----------
    root : Path
        The ovx directory, ``~/.ovx`` unless ``$OVX_DIR`` says otherwise.
    config_file : Path
        The TOML file holding one table per profile.
    token_dir : Path
        Directory holding one stored login per profile.
    """

    root: Path
    config_file: Path
    token_dir: Path

    @classmethod
    def resolve(cls, environ: dict[str, str] | None = None) -> Locations:
        """Work out the locations from the environment.

        ``$OVX_CONFIG_FILE`` overrides only the config file, so a caller can
        point at one config while keeping the default token directory.

        Parameters
        ----------
        environ :
            Environment to read. Defaults to the real one.

        Returns
        -------
        Locations
            The resolved paths.
        """
        env = os.environ if environ is None else environ
        # An empty $HOME must not yield a relative root: that would write a
        # live token into whatever directory you happened to be in.
        home = env.get("HOME") or str(Path.home())
        root = Path(env.get("OVX_DIR") or Path(home) / ".ovx")
        config_file = Path(env.get("OVX_CONFIG_FILE") or root / "config.toml")
        return cls(
            root=root.expanduser(),
            config_file=config_file.expanduser(),
            token_dir=(root / "tokens").expanduser(),
        )
