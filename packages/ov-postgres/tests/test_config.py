"""Unit tests for backend settings that need no database.

``PgVectorParams`` is a plain pydantic model and ``from_config`` connects
lazily, so the mapping from ``custom_params`` onto adapter state is testable
without PostgreSQL.
"""

from __future__ import annotations

from typing import Any

import pytest
from openviking_cli.utils.config.vectordb_config import VectorDBBackendConfig
from pydantic import ValidationError

from ov_postgres.adapter import PgVectorCollectionAdapter
from ov_postgres.collection import _term_tsquery
from ov_postgres.config import DEFAULT_KEYWORD_FIELDS, PgVectorParams

DSN = "postgresql://user:pw@localhost:5432/openviking"


def make_adapter(**params: Any) -> PgVectorCollectionAdapter:
    """Build an unconnected adapter from ``custom_params``."""
    config = VectorDBBackendConfig(
        backend="ov_postgres.adapter.PgVectorCollectionAdapter",
        name="context",
        index_name="default",
        custom_params={"dsn": DSN, **params},
    )
    return PgVectorCollectionAdapter.from_config(config)


def test_store_content_defaults_to_false() -> None:
    """Bodies are dropped unless asked for, matching every non-VikingDB backend."""
    assert PgVectorParams(dsn=DSN).store_content is False
    assert make_adapter().USE_CONTENT_FIELD is False


def test_store_content_true_sets_the_flag_openviking_reads() -> None:
    """OpenViking gates content materialisation on this exact attribute."""
    assert make_adapter(store_content=True).USE_CONTENT_FIELD is True


def test_store_content_is_per_instance_not_per_class() -> None:
    """One collection storing bodies must not switch it on for every other.

    ``__init__`` assigns to ``self.USE_CONTENT_FIELD``, which shadows the class
    attribute. Assigning to the class instead would leak across adapters and
    silently start writing bodies for a collection that never asked.
    """
    storing = make_adapter(store_content=True)
    plain = make_adapter()

    assert storing.USE_CONTENT_FIELD is True
    assert plain.USE_CONTENT_FIELD is False
    assert PgVectorCollectionAdapter.USE_CONTENT_FIELD is False


def test_store_content_rejects_a_non_boolean() -> None:
    """A typo'd value fails at startup rather than defaulting to off."""
    with pytest.raises(ValidationError):
        PgVectorParams(dsn=DSN, store_content="yes please")


def test_unknown_option_is_still_rejected() -> None:
    """Adding a field must not loosen ``extra="forbid"``."""
    with pytest.raises(ValidationError):
        PgVectorParams(dsn=DSN, store_contents=True)  # type: ignore[call-arg]


def test_oversized_text_is_truncated_before_the_write() -> None:
    """A body too large for a tsvector must be cut, not left to fail the INSERT.

    The full-text index computes ``to_tsvector`` on every write, and PostgreSQL
    refuses one built from more than 1048575 bytes -- which aborts the whole
    statement. Two truncated fields plus the shorter keyword columns have to
    fit under that.
    """
    adapter = make_adapter(store_content=True)
    limit = adapter._TEXT_FIELD_BYTE_LIMIT
    assert limit is not None, "no limit means no truncation, and a failed write"

    record = adapter._normalize_record_for_write(
        {"id": "x", "content": "a" * (limit * 2), "abstract": "b" * (limit * 2)}
    )

    assert len(record["content"].encode("utf-8")) <= limit
    assert len(record["abstract"].encode("utf-8")) <= limit
    total = sum(len(record[f].encode("utf-8")) for f in ("content", "abstract"))
    assert total < 1_048_575, "both fields share one tsvector"


def test_truncation_does_not_split_a_character() -> None:
    """Cutting mid-character would store bytes that are not valid UTF-8."""
    adapter = make_adapter(store_content=True)
    limit = adapter._TEXT_FIELD_BYTE_LIMIT

    # Three bytes each, so the limit falls inside a character rather than
    # between two.
    record = adapter._normalize_record_for_write({"id": "x", "content": "€" * limit})

    truncated = record["content"]
    assert len(truncated.encode("utf-8")) <= limit
    assert set(truncated) == {"€"}, "a split character would decode to something else"


def test_truncation_applies_without_store_content() -> None:
    """``abstract`` is a default keyword field, so it can overflow on its own."""
    adapter = make_adapter()
    limit = adapter._TEXT_FIELD_BYTE_LIMIT

    record = adapter._normalize_record_for_write(
        {"id": "x", "abstract": "a" * (limit * 2)}
    )

    assert len(record["abstract"].encode("utf-8")) <= limit


def test_store_content_puts_bodies_in_the_keyword_fields() -> None:
    """Storing bodies and not searching them would pay for the column twice."""
    params = PgVectorParams(dsn=DSN, store_content=True)

    fields = params.resolved_keyword_fields()

    assert "content" in fields
    assert fields[: len(DEFAULT_KEYWORD_FIELDS)] == list(DEFAULT_KEYWORD_FIELDS)


def test_bodies_stay_out_of_the_index_when_not_stored() -> None:
    """An unpopulated column in the tsvector is index maintenance for nothing."""
    assert "content" not in PgVectorParams(dsn=DSN).resolved_keyword_fields()


def test_explicit_keyword_fields_win_over_the_content_default() -> None:
    """Storing bodies without indexing them has to remain expressible."""
    params = PgVectorParams(
        dsn=DSN, store_content=True, keyword_fields=["name", "abstract"]
    )

    assert params.resolved_keyword_fields() == ["name", "abstract"]


def test_any_mode_turns_the_conjunction_into_a_disjunction() -> None:
    """`plainto_tsquery` ANDs every word, which no long query survives."""
    rendered = _term_tsquery("any", "english").as_string(None)

    assert "regexp_replace" in rendered
    assert "'&', '|'" in rendered
    # An all-stopword term parses to '', and to_tsquery('') raises.
    assert "nullif" in rendered
    assert rendered.count("%s") == 1, "placeholder count must not vary by mode"


def test_all_mode_keeps_every_word_required() -> None:
    """A deliberate keyword phrase should still match as a phrase."""
    rendered = _term_tsquery("all", "english").as_string(None)

    assert "plainto_tsquery" in rendered
    assert "regexp_replace" not in rendered
    assert rendered.count("%s") == 1


def test_keyword_defaults_follow_memex() -> None:
    """OR matching and cover-density ranking are the useful defaults."""
    params = PgVectorParams(dsn=DSN)

    assert params.keyword_query_mode == "any"
    assert params.keyword_rank == "ts_rank_cd"


def test_unknown_rank_function_is_rejected() -> None:
    """Never let a config string reach SQL unchecked."""
    with pytest.raises(ValidationError):
        PgVectorParams(dsn=DSN, keyword_rank="ts_rank_bm25")
