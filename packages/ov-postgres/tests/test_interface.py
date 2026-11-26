"""Unit tests for how this backend meets OpenViking's interfaces. No database.

OpenViking adds keyword arguments to ``ICollection`` between minor releases,
and its ``Collection`` facade forwards them by name. A backend that lags one
raises ``TypeError`` on every call through the facade, so the signatures are
checked here rather than discovered in production.
"""

from __future__ import annotations

import inspect

import pytest
from openviking.storage.vectordb.collection.collection import ICollection
from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig

from ov_postgres.adapter import MODE, PgVectorCollectionAdapter
from ov_postgres.collection import PgVectorCollection, _refuse_advance

_ABSTRACT_METHODS = sorted(
    name
    for name, member in vars(ICollection).items()
    if getattr(member, "__isabstractmethod__", False)
)


def make_adapter() -> PgVectorCollectionAdapter:
    """Build an unconnected adapter."""
    config = VectorDBBackendConfig(
        backend="ov_postgres.adapter.PgVectorCollectionAdapter",
        name="context",
        index_name="default",
        custom_params={"dsn": "postgresql://user:pw@localhost:5432/openviking"},
    )
    return PgVectorCollectionAdapter.from_config(config)


def test_the_interface_still_declares_the_methods_checked_below() -> None:
    """Guards the parametrisation: an empty list would check nothing."""
    assert {"search_by_keywords", "search_by_random", "search_by_vector"} <= set(
        _ABSTRACT_METHODS
    )


@pytest.mark.parametrize("name", _ABSTRACT_METHODS)
def test_every_interface_parameter_is_accepted(name: str) -> None:
    """Each ``ICollection`` method here takes every parameter the interface names."""
    declared = inspect.signature(getattr(ICollection, name)).parameters
    ours = inspect.signature(getattr(PgVectorCollection, name)).parameters
    missing = [param for param in declared if param not in ours]
    assert not missing, f"PgVectorCollection.{name} lacks {missing}"


def test_get_meta_data_accepts_raise_on_error() -> None:
    """The facade passes ``raise_on_error`` by keyword; the interface omits it."""
    params = inspect.signature(PgVectorCollection.get_meta_data).parameters
    assert params["raise_on_error"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_mode_is_not_openvikings_own_pgvector_backend() -> None:
    """OpenViking branches on ``mode == "pgvector"`` for its native backend."""
    adapter = make_adapter()
    assert adapter.mode == MODE
    assert adapter.mode != "pgvector"


def test_event_time_decay_is_refused_by_name_before_any_query() -> None:
    """Time decay fails up front, naming this backend, instead of reaching SQL.

    Under ``mode == "pgvector"`` OpenViking passes the raw decay request on to
    the collection, expecting its native backend to apply it; this adapter
    would then try to connect. Refused here, nothing connects at all.
    """
    adapter = make_adapter()
    with pytest.raises(NotImplementedError, match=f"not supported by {MODE}"):
        adapter.query(
            query_vector=[0.0] * 8,
            advance={
                "time_decay": {
                    "protection": "7d",
                    "origin": "2026-01-01T00:00:00+00:00",
                }
            },
        )


@pytest.mark.parametrize("advance", [None, {}])
def test_no_advance_options_are_accepted(advance: dict[str, object] | None) -> None:
    """The facade passes ``advance=None``; that must not raise."""
    _refuse_advance("search_by_random", advance)


def test_advance_options_are_refused_by_name() -> None:
    """An engine-side option this backend lacks fails rather than being ignored."""
    with pytest.raises(NotImplementedError, match="post_process_ops, time_decay"):
        _refuse_advance(
            "search_by_vector", {"time_decay": {}, "post_process_ops": []}
        )
