"""Run OpenViking's server with every ov-ext subsystem installed.

OpenViking has no plugin mechanism: its server is the console script
``openviking-server``, which calls ``openviking_cli.server_bootstrap:main``
directly. Something therefore has to call :func:`ov_ext.install` before
the first search, and the honest place is a wrapper that does exactly that and
then hands over.

Installed as the ``ov-ext-server`` console script, so a deployment swaps
one command for another::

    openviking-server --config /etc/ov.conf     # stock
    ov-ext-server --config /etc/ov.conf         # + retrieval, + reflection

Arguments are passed through untouched, including subcommands such as
``ingest``, since ``main`` reads ``sys.argv`` itself.

The alternative -- a ``sitecustomize`` module or a ``.pth`` file -- would patch
every Python process on the machine, invisibly. A wrapper keeps the change
where an operator can see it in the process list.
"""

from __future__ import annotations

import logging

from .installer import install

__all__ = ["main"]

logger = logging.getLogger(__name__)


def main() -> None:
    """Install every subsystem, then run OpenViking's server.

    Settings come from the environment, so the wrapper needs no arguments of
    its own and cannot shadow one of OpenViking's.

    Raises
    ------
    RuntimeError
        If a subsystem cannot attach. Deliberately fatal: a server that
        silently started without its keyword leg, or without reflection
        registered, would look healthy and behave worse -- far harder to notice
        than a refused startup.
    """
    install()

    from openviking_cli.server_bootstrap import main as server_main

    server_main()


if __name__ == "__main__":
    main()
