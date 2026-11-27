"""Shared fixtures.

The span fixtures here stand in for OpenViking's server, which installs the
process-global ``TracerProvider`` in production. This package installs none of
its own, so without something playing that part its spans record nothing --
which is the behaviour ``test_observability.py`` also checks for directly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture(scope="session")
def _tracer_provider() -> InMemorySpanExporter:
    """Attach an in-memory exporter to the session's TracerProvider.

    Session-scoped because ``set_tracer_provider`` takes the first provider it
    is given and only warns about the rest.

    An existing provider is reused rather than replaced. Run this suite in the
    same process as ov-postgres's -- ``uv run pytest`` across the workspace
    does exactly that -- and the second call would be ignored, leaving this
    exporter permanently empty. The failure that hides is worse than the ones
    it causes: a test asserting *no* spans were recorded passes against a dead
    exporter for entirely the wrong reason.

    ``SimpleSpanProcessor`` rather than the batching one, so a span is
    exported when it ends instead of on a timer a test would have to wait for.
    """
    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    provider = current if isinstance(current, TracerProvider) else TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    if provider is not current:
        trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(_tracer_provider: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    """Capture spans finished during one test, discarding earlier ones."""
    _tracer_provider.clear()
    yield _tracer_provider
    _tracer_provider.clear()


@dataclass(frozen=True)
class _VectorDBSettings:
    """The one VectorDB setting OpenViking reads for a shared collection."""

    dimension: int


@dataclass(frozen=True)
class _AccountVectorSettings:
    """What ``AccountVectorConfigResolver.resolve`` hands back, reduced to its use."""

    vectordb: _VectorDBSettings
    dedicated_vectordb: bool = False


class SharedVectorConfig:
    """Resolve every account to the shared collection, as a default server does.

    OpenViking 0.5 resolves vector settings per account, and the backend
    refuses any per-account call until ``OpenVikingService.initialize`` has
    set a resolver. The real one reads account settings through the runtime
    config manager and AGFS, which a test that builds a bare backend has none
    of.

    Parameters
    ----------
    dimension :
        The embedding dimension of the test collection.
    """

    def __init__(self, dimension: int) -> None:
        self._settings = _AccountVectorSettings(_VectorDBSettings(dimension))

    async def resolve(self, account_id: str) -> _AccountVectorSettings:
        """Return the shared-collection settings for any account."""
        return self._settings


@pytest.fixture
def attach_vector_config() -> Callable[[Any, int], Any]:
    """Return a function giving a bare backend the resolver the service would set."""

    def attach(store: Any, dimension: int) -> Any:
        store.set_vector_config_resolver(SharedVectorConfig(dimension))
        return store

    return attach


@pytest.fixture(scope="session", autouse=True)
def _empty_openviking_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Point OpenViking at an empty config file for the whole session.

    OpenViking 0.5's ``VikingVectorIndexBackend`` reads the global config on
    construction, to set up its native logging. Left alone it would read the
    developer's own ``~/.openviking/ov.conf``, or fail where there is none, as
    on CI. An empty file gives OpenViking's defaults everywhere.
    """
    path = tmp_path_factory.mktemp("openviking") / "ov.conf"
    path.write_text("{}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OPENVIKING_CONFIG_FILE", str(path))
        yield
