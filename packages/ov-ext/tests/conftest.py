"""Shared fixtures.

The span fixtures here stand in for OpenViking's server, which installs the
process-global ``TracerProvider`` in production. This package installs none of
its own, so without something playing that part its spans record nothing --
which is the behaviour ``test_observability.py`` also checks for directly.
"""

from __future__ import annotations

from collections.abc import Iterator

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
