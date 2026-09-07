"""Run OpenViking's server with hybrid retrieval installed.

OpenViking has no plugin mechanism: its server is the console script
``openviking-server``, which calls ``openviking_cli.server_bootstrap:main``
directly. Something therefore has to call :func:`ov_retrieval.install` before
the first search, and the honest place is a wrapper that does exactly that and
then hands over.

Installed as the ``ov-retrieval-server`` console script, so a deployment swaps
one command for another::

    openviking-server --config /etc/ov.conf     # vector only
    ov-retrieval-server --config /etc/ov.conf   # keyword leg + diversity

Arguments are passed through untouched, including subcommands such as
``ingest``, since ``main`` reads ``sys.argv`` itself.

The alternative -- a ``sitecustomize`` module or a ``.pth`` file -- would patch
every Python process on the machine, invisibly. A wrapper keeps the change
where an operator can see it in the process list.
"""

from __future__ import annotations

import logging

from .config import HybridSettings
from .install import install

__all__ = ["main"]

logger = logging.getLogger(__name__)


def main() -> None:
    """Install hybrid retrieval, then run OpenViking's server.

    Settings come from the environment, so the wrapper needs no arguments of
    its own and cannot shadow one of OpenViking's.

    Raises
    ------
    RuntimeError
        If the retriever cannot be patched. Deliberately fatal: a server that
        silently started without its keyword leg would look healthy and answer
        worse, which is far harder to notice than a refused startup.
    """
    settings = HybridSettings()
    install(settings)
    logger.info(
        "ov-retrieval: keyword=%s mmr=%s lambda=%.2f pool=x%d",
        settings.keyword_enabled,
        settings.mmr_enabled,
        settings.mmr_lambda,
        settings.pool_factor,
    )

    from openviking_cli.server_bootstrap import main as server_main

    server_main()


if __name__ == "__main__":
    main()
