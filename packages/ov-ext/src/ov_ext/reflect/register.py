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


def register(settings: ReflectSettings | None = None) -> None:
    """Make the ``observations`` memory type available to OpenViking.

    Does nothing when reflection is disabled, which is the default: a memory
    type that nothing writes is clutter in someone's schema listing.

    Parameters
    ----------
    settings :
        Behaviour toggles. Read from the environment when omitted.
    """
    global _installed
    resolved = settings or ReflectSettings()
    if not resolved.enabled:
        logger.debug("ov-ext reflect: disabled, memory type not registered")
        return

    schema = TEMPLATES_DIR / "observations.yaml"
    configured = _configured_templates_dir()

    if configured is None:
        _set_templates_dir(TEMPLATES_DIR)
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
    global _installed
    if _installed is not None and _installed.exists():
        _installed.unlink()
    _installed = None


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


def _set_templates_dir(path: Path) -> None:
    """Point OpenViking's custom memory templates dir at ``path``."""
    from openviking_cli.utils.config import get_openviking_config

    get_openviking_config().memory.custom_templates_dir = str(path)
