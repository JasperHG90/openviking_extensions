"""Configuration and credentials for ov-sync.

Two things are configured here, from two different places.

Sync behavior (what to send, where to, what to leave out) comes from
``ov-sync.toml`` in the synced folder, overridable by ``OV_SYNC_*`` environment
variables. Server credentials come from the OpenViking CLI's own config file,
so ``ov`` and ``ovsync`` talk to the same server without a second copy of the
API key on disk.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

CONFIG_FILENAME = "ov-sync.toml"
DEFAULT_STATE_FILE = ".ov-sync.db"

# Server-side ceilings, mirrored here so a batch is split before the request
# goes out rather than after it is refused. openviking/storage/content_write.py
# holds the authoritative values; these track it.
SERVER_MAX_OPERATIONS = 256
SERVER_MAX_FILE_BYTES = 8 * 1024 * 1024
SERVER_MAX_TOTAL_BYTES = 16 * 1024 * 1024

DEFAULT_BASE_EXCLUDES = [
    ".git",
    ".obsidian",
    ".trash",
    ".venv",
    "__pycache__",
    "node_modules",
]


class ExcludeConfig(BaseModel):
    """Rules for leaving files out of a sync."""

    model_config = {"extra": "forbid"}

    base: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BASE_EXCLUDES),
        description="Glob patterns always excluded, matched against each path "
        "component and against the whole relative path.",
    )
    extends_exclude: list[str] = Field(
        default_factory=list,
        description='Extra glob patterns beyond the base set, e.g. "templates/**".',
    )
    ignore_folders: list[str] = Field(
        default_factory=list,
        description="Folder names never synced. Unlike the glob patterns these "
        'match an exact folder name at any depth, e.g. ["private", "scratch"].',
    )
    frontmatter_skip_key: str = Field(
        default="ov",
        description="Frontmatter key checked to skip a Markdown file.",
    )
    frontmatter_skip_value: str = Field(
        default="skip",
        description="Value of the skip key that keeps a file out of the sync. "
        'With the defaults, a note carrying "ov: skip" is never sent. '
        "The comparison ignores case.",
    )

    @property
    def all_patterns(self) -> list[str]:
        """Base patterns plus the user's additions."""
        return self.base + self.extends_exclude


class SyncConfig(BaseModel):
    """What ov-sync sends, and where it lands."""

    model_config = {"extra": "forbid"}

    root_uri: str | None = Field(
        default=None,
        description="Target directory URI. Every synced file lands at "
        "<root_uri>/<path relative to the folder>. Must already exist.",
    )
    state_file: str = Field(
        default=DEFAULT_STATE_FILE,
        description="Name of the SQLite file, kept in the synced folder's root. "
        "It records what was sent, so the next run only sends what changed.",
    )
    include_extensions: list[str] = Field(
        default_factory=lambda: [
            ".md",
            ".txt",
            ".csv",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".html",
            ".pdf",
        ],
        description="File extensions to sync. Anything else is ignored. "
        "An empty list means every file, subject to the exclude rules.",
    )
    exclude: ExcludeConfig = Field(
        default_factory=ExcludeConfig,
        description="Rules for leaving files out.",
    )
    max_operations_per_batch: int = Field(
        default=SERVER_MAX_OPERATIONS,
        ge=1,
        le=SERVER_MAX_OPERATIONS,
        description="Files per batch-write request. Lower it to see progress "
        "sooner or to keep each server-side reindex smaller.",
    )
    wait_for_indexing: bool = Field(
        default=True,
        description="Wait for the server to finish reindexing each batch. "
        "Turning this off returns sooner but leaves new content unsearchable "
        "until the server catches up.",
    )
    request_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Read timeout for a batch-write. Indexing happens inside "
        "the request when wait_for_indexing is set, so this is not a fast call.",
    )

    @field_validator("include_extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        """Lower-case each extension and give it a leading dot."""
        return [
            ext if ext.startswith(".") else f".{ext}"
            for ext in (e.lower() for e in value)
        ]


class WatchConfig(BaseModel):
    """Settings for continuous sync."""

    model_config = {"extra": "forbid"}

    mode: str = Field(
        default="events",
        pattern="^(events|poll)$",
        description='"events" reacts to filesystem events; "poll" rescans on a timer.',
    )
    debounce_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Event mode: quiet period after the last event before syncing, "
        "so a burst of editor saves becomes one sync.",
    )
    poll_interval_seconds: float = Field(
        default=300.0,
        ge=10,
        description="Poll mode: seconds between scans.",
    )
    reconcile_interval_seconds: float = Field(
        default=900.0,
        ge=30,
        description="Event mode: seconds between full rescans. Events alone "
        "cannot be trusted to be complete — watchdog drops them when its "
        "queue overflows, and anything edited while the watcher was stopped "
        "produced no event at all. The rescan is what catches up.",
    )


class OvSyncConfig(BaseSettings):
    """Root configuration, read from ``ov-sync.toml`` and ``OV_SYNC_*`` env vars.

    Build one with :func:`load_config` rather than calling the constructor:
    ``load_config`` is what finds and reads the TOML file.

    Examples
    --------
    ``OV_SYNC_SYNC__ROOT_URI=viking://resources/notes`` overrides ``root_uri``
    under the ``[sync]`` table; the double underscore is the nesting separator.
    """

    model_config = SettingsConfigDict(
        env_prefix="OV_SYNC_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    sync: SyncConfig = Field(
        default_factory=SyncConfig,
        description="What to send and where it lands.",
    )
    watch: WatchConfig = Field(
        default_factory=WatchConfig,
        description="Continuous-sync settings.",
    )


class _MappingSource(PydanticBaseSettingsSource):
    """Settings source serving a mapping that was already parsed from disk.

    Parameters
    ----------
    settings_cls :
        The settings class being built, as pydantic-settings requires.
    data :
        Parsed contents of the TOML config file.
    """

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[object, str, bool]:
        """Unused: this source supplies the whole mapping at once."""
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        """Return the file's contents as settings values."""
        return self._data


def find_config_file(folder: Path, config_path: Path | None = None) -> Path | None:
    """Locate the TOML config for a folder.

    Parameters
    ----------
    folder :
        The folder being synced.
    config_path :
        An explicit file, which wins when given.

    Returns
    -------
    Path or None
        The first file that exists: ``<folder>/ov-sync.toml``, then
        ``~/.config/ov-sync/ov-sync.toml``. None when there is no config
        anywhere, which is fine — every field except ``root_uri`` has a
        default.

    Raises
    ------
    FileNotFoundError
        If ``config_path`` was given and does not exist. Falling back to the
        search would silently sync with settings the caller did not ask for.
    """
    if config_path is not None:
        if not config_path.is_file():
            raise FileNotFoundError(f"No config file at {config_path}")
        return config_path

    home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    candidates = [folder / CONFIG_FILENAME, home / "ov-sync" / CONFIG_FILENAME]
    return next((path for path in candidates if path.is_file()), None)


def load_config(folder: Path, config_path: Path | None = None) -> OvSyncConfig:
    """Load the sync configuration for a folder.

    Precedence, highest first: ``OV_SYNC_*`` environment variables, then the
    TOML file, then the field defaults.

    Parameters
    ----------
    folder :
        The folder being synced. Its root is searched for ``ov-sync.toml``.
    config_path :
        An explicit config file, which wins over the search.

    Returns
    -------
    OvSyncConfig
        The merged configuration.

    Raises
    ------
    ValueError
        If the TOML file is malformed, or holds a key the schema does not
        define — a typo'd key is a silently ignored setting otherwise.
    """
    path = find_config_file(folder, config_path)
    data: dict[str, Any] = {}
    if path is not None:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} is not valid TOML: {exc}") from exc

    # A local subclass is what keeps the file's data out of module-level state:
    # every call builds its own source, so two folders can be loaded at once.
    class _FileBackedConfig(OvSyncConfig):
        """An OvSyncConfig whose file layer is the mapping ``data`` holds."""

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            """Order the sources: env beats the file, the file beats defaults."""
            return (env_settings, _MappingSource(settings_cls, data))

    return _FileBackedConfig()


class Credentials(BaseModel):
    """How to reach an OpenViking server.

    Attributes
    ----------
    url : str
        Server base URL, without a trailing slash.
    api_key : str
        Bearer token sent on every request.
    account : str
        Value for the ``X-OpenViking-Account`` header.
    user : str
        Value for the ``X-OpenViking-User`` header.
    """

    model_config = {"extra": "ignore"}

    url: str = Field(description="Server base URL.")
    api_key: str = Field(description="Bearer token.")
    account: str = Field(default="", description="X-OpenViking-Account header.")
    user: str = Field(default="", description="X-OpenViking-User header.")

    @field_validator("url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """Drop a trailing slash so paths can be joined without doubling it."""
        return value.rstrip("/")


def cli_config_path() -> Path:
    """Return the OpenViking CLI's config file path.

    ``$OPENVIKING_CLI_CONFIG_FILE`` wins when set, which is how ``ovx`` points
    ``ov`` at a per-profile config; honoring it here means ``ovx run <profile>
    -- ovsync ...`` reaches the same server as ``ovx run <profile> -- ov ...``.
    """
    override = os.environ.get("OPENVIKING_CLI_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".openviking" / "ovcli.conf"


def load_credentials(path: Path | None = None) -> Credentials:
    """Read server credentials from the OpenViking CLI config, then the environment.

    ``OV_SYNC_URL``, ``OV_SYNC_API_KEY``, ``OV_SYNC_ACCOUNT`` and
    ``OV_SYNC_USER`` each override the matching file field, so a key can stay
    out of the file entirely.

    Parameters
    ----------
    path :
        Config file to read. Defaults to :func:`cli_config_path`.

    Returns
    -------
    Credentials
        The resolved connection settings.

    Raises
    ------
    FileNotFoundError
        If no config file exists and the environment does not supply both a
        URL and an API key.
    ValueError
        If the file exists but is not valid JSON, or the merged result is
        missing a URL or an API key.
    """
    config_file = cli_config_path() if path is None else path
    data: dict[str, Any] = {}
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{config_file} is not valid JSON: {exc}") from exc
    elif not (os.environ.get("OV_SYNC_URL") and os.environ.get("OV_SYNC_API_KEY")):
        raise FileNotFoundError(
            f"No OpenViking config at {config_file}. Run `ov config` to create one, "
            "or set OV_SYNC_URL and OV_SYNC_API_KEY."
        )

    for field, env_var in (
        ("url", "OV_SYNC_URL"),
        ("api_key", "OV_SYNC_API_KEY"),
        ("account", "OV_SYNC_ACCOUNT"),
        ("user", "OV_SYNC_USER"),
    ):
        value = os.environ.get(env_var)
        if value:
            data[field] = value

    if not data.get("url") or not data.get("api_key"):
        raise ValueError(
            f"{config_file} has no url or api_key, and the environment does not "
            "supply them. Run `ov config` or set OV_SYNC_URL and OV_SYNC_API_KEY."
        )
    return Credentials.model_validate(data)


DEFAULT_CONFIG_TOML = """\
# Where this folder lands in OpenViking. The directory must already exist:
# create it with `ov mkdir <uri>`. Every file is written to
# <root_uri>/<its path relative to this folder>.
[sync]
root_uri = ""

# Extensions to sync. Anything else is ignored.
include_extensions = [
    ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".html", ".pdf",
]

# Files per batch-write request. The server caps this at 256.
max_operations_per_batch = 256

# Wait for the server to index each batch before moving on. Turn it off for a
# faster run that leaves new content unsearchable for a while.
wait_for_indexing = true

[sync.exclude]
# Glob patterns, matched against every path component and the whole path.
base = [".git", ".obsidian", ".trash", ".venv", "__pycache__", "node_modules"]
extends_exclude = []

# Exact folder names, at any depth.
ignore_folders = []

# A Markdown file whose frontmatter carries `ov: skip` is never sent.
frontmatter_skip_key = "ov"
frontmatter_skip_value = "skip"

[watch]
# "events" reacts to filesystem events; "poll" rescans on a timer.
mode = "events"
debounce_seconds = 5
poll_interval_seconds = 300

# Event mode also rescans this often, to catch anything the events missed.
reconcile_interval_seconds = 900
"""
