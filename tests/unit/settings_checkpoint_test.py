"""Settings validators for checkpoint / resumable-indexing fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from book_graph_rag.config import Settings


def _settings(**overrides: object) -> Settings:
    """Build a minimal Settings object with valid defaults."""
    base = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    return Settings.model_validate({**base, **overrides})


@pytest.mark.parametrize("value", [9, 86401, -1, 0])
def test_checkpoint_stale_lease_seconds_out_of_range(value: int) -> None:
    """Lease timeout must be between 10 seconds and 24 hours (inclusive)."""
    with pytest.raises(ValidationError):
        _settings(checkpoint_stale_lease_seconds=value)


@pytest.mark.parametrize("value", [10, 300, 86400])
def test_checkpoint_stale_lease_seconds_in_range(value: int) -> None:
    """Boundary and typical values inside the allowed range are accepted."""
    settings = _settings(checkpoint_stale_lease_seconds=value)
    assert settings.checkpoint_stale_lease_seconds == value


@pytest.mark.parametrize("value", [0, 11, -3])
def test_checkpoint_max_attempts_out_of_range(value: int) -> None:
    """Retry budget must be between 1 and 10 (inclusive)."""
    with pytest.raises(ValidationError):
        _settings(checkpoint_max_attempts=value)


@pytest.mark.parametrize("value", [1, 3, 10])
def test_checkpoint_max_attempts_in_range(value: int) -> None:
    settings = _settings(checkpoint_max_attempts=value)
    assert settings.checkpoint_max_attempts == value


@pytest.mark.parametrize("field", ["pipeline_version", "schema_version"])
@pytest.mark.parametrize("value", ["1.0", "1.0.0-alpha", "v1.0.0", "", "1.2.3.4"])
def test_semantic_version_rejects_invalid(field: str, value: str) -> None:
    """pipeline_version and schema_version must be exact x.y.z strings."""
    with pytest.raises(ValidationError):
        _settings(**{field: value})


@pytest.mark.parametrize("field", ["pipeline_version", "schema_version"])
@pytest.mark.parametrize("value", ["1.0.0", "0.0.1", "12.34.56"])
def test_semantic_version_accepts_valid(field: str, value: str) -> None:
    settings = _settings(**{field: value})
    assert getattr(settings, field) == value


@pytest.mark.parametrize("value", ["2026-09-1", "09-01-2026", "2026/09/01", "2026-13-01", ""])
def test_graph_llm_model_date_rejects_invalid(value: str) -> None:
    """graph_llm_model_date must be ISO yyyy-mm-dd."""
    with pytest.raises(ValidationError):
        _settings(graph_llm_model_date=value)


@pytest.mark.parametrize("value", ["2026-09-01", "2024-02-29"])
def test_graph_llm_model_date_accepts_valid(value: str) -> None:
    settings = _settings(graph_llm_model_date=value)
    assert settings.graph_llm_model_date == value


def test_checkpoint_defaults() -> None:
    """The resumable-indexing defaults match the design."""
    settings = _settings()
    assert settings.checkpoint_enabled is True
    assert settings.checkpoint_stale_lease_seconds == 300
    assert settings.checkpoint_max_attempts == 3
    assert settings.pipeline_version == "1.0.0"
    assert settings.schema_version == "1.0.0"
    assert settings.graph_llm_model_date == "2026-09-01"
