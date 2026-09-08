"""Make OpenViking aware of the ``observations`` memory type.

Unlike retrieval, this is not a monkeypatch. OpenViking's
``MemoryTypeRegistry`` loads memory schemas from YAML and reads a configured
``memory.custom_templates_dir`` alongside its own bundled ones -- a documented
extension point, used as documented.

The only awkward part is that the setting is a *directory*, and there is one of
it. If a deployment already points it somewhere, that directory wins and this
one is not read; overwriting the operator's choice to install our own would be
worse than declining to. In that case the schema is copied into the directory
they chose, so it registers without the setting changing.

Setting it when it is empty is not free either: ``MemoryTypeRegistry`` falls
back to ``resolve_memory_templates_dir()`` when the custom dir is unset, so
filling it in suppresses that fallback. Whatever was there before is recorded
and put back by :func:`unregister`.

Registration is idempotent and does not start anything. A sweep runs when
something calls :meth:`ov_ext.reflect.engine.ReflectionEngine.sweep` -- from a
cron, a CLI, or a test -- rather than on a timer inside the server. Reflection
writes to memory, and a thing that writes to memory should run when someone
decided it should.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import ReflectSettings

__all__ = ["TEMPLATES_DIR", "register", "unregister"]

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Set by register() so unregister() removes only a file we installed.
_installed: Path | None = None

# The value of custom_templates_dir before register() changed it, so
# unregister() can put an operator's configuration back rather than leaving
# ours in place. A sentinel distinguishes "not changed" from "was empty".
_UNSET: object = object()
_previous_dir: str | None | object = _UNSET


def register(settings: ReflectSettings | None = None) -> None:
    """Make the ``observations`` memory type available to OpenViking.

    Does nothing when reflection is disabled, which is the default: a memory
    type that nothing writes is clutter in someone's schema listing.

    Parameters
    ----------
    settings :
        Behaviour toggles. Read from the environment when omitted.
    """
    global _installed, _previous_dir
    resolved = settings or ReflectSettings()
    if not resolved.enabled:
        logger.debug("ov-ext reflect: disabled, memory type not registered")
        return

    schema = TEMPLATES_DIR / "observations.yaml"
    configured = _configured_templates_dir()

    if configured is None:
        _previous_dir = _current_templates_dir()
        _set_templates_dir(str(TEMPLATES_DIR))
        logger.info("ov-ext reflect: memory templates dir set to %s", TEMPLATES_DIR)
        return

    if configured.resolve() == TEMPLATES_DIR.resolve():
        return

    # Someone else already owns the directory. Copy in rather than redirect.
    configured.mkdir(parents=True, exist_ok=True)
    destination = configured / schema.name
    shutil.copyfile(schema, destination)
    _installed = destination
    logger.info("ov-ext reflect: installed %s into %s", schema.name, configured)


def unregister() -> None:
    """Undo :func:`register`, leaving an operator's own directory as it was.

    Only removes a schema this process copied in. A directory that was already
    configured keeps everything else it holds, and a setting this process did
    not change is left alone.
    """
    global _installed, _previous_dir
    if _installed is not None and _installed.exists():
        _installed.unlink()
    _installed = None
    if _previous_dir is not _UNSET:
        _set_templates_dir(_previous_dir)  # type: ignore[arg-type]
        _previous_dir = _UNSET


def _configured_templates_dir() -> Path | None:
    """Return OpenViking's configured custom memory templates dir, if any."""
    try:
        from openviking_cli.utils.config import get_openviking_config

        configured = get_openviking_config().memory.custom_templates_dir
    except Exception:
        # No OpenViking config in this process -- a unit test, or a CLI that
        # never booted the server. Nothing to defer to.
        return None
    return Path(configured) if configured else None


def _current_templates_dir() -> str | None:
    """Return the raw configured value, or ``None`` when there is no config."""
    try:
        from openviking_cli.utils.config import get_openviking_config

        return str(get_openviking_config().memory.custom_templates_dir)
    except Exception:
        return None


def _set_templates_dir(path: str | None) -> None:
    """Point OpenViking's custom memory templates dir at ``path``."""
    from openviking_cli.utils.config import get_openviking_config

    get_openviking_config().memory.custom_templates_dir = path or ""
