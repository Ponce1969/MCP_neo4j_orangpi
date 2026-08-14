"""Tests for book_graph_rag.config.Settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from book_graph_rag.config import Settings


def _clear_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove required Neo4j env vars so tests are independent of the shell."""
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


def test_settings_fails_fast_without_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01.2: Settings() must fail fast when no .env or env vars are present."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings()


def test_settings_loads_secret_securely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01.3: SecretStr value is reachable but never exposed by repr/str."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "a-real-secret",
    }
    settings = Settings.model_validate(data)

    assert settings.neo4j_password.get_secret_value() == "a-real-secret"
    assert "a-real-secret" not in repr(settings.neo4j_password)
    assert "a-real-secret" not in str(settings.neo4j_password)


def test_settings_overlap_must_be_less_than_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pdf_chunk_overlap must be strictly less than pdf_max_chunk_size."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "pdf_max_chunk_size": 1500,
        "pdf_chunk_overlap": 2000,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "menor que pdf_max_chunk_size" in str(exc_info.value)


def test_settings_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check: non-secret fields keep their documented defaults."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    settings = Settings.model_validate(data)

    assert settings.pdf_max_chunk_size == 1500
    assert settings.pdf_chunk_overlap == 150
    assert settings.llm_max_concurrency == 3
    assert settings.processing_batch_size == 5
    assert settings.dead_letter_path == Path("data/dead_letter.log")
    assert settings.llm_max_retries == 5
    assert settings.llm_retry_wait_multiplier == 1.0
    assert settings.llm_retry_wait_max == 30.0
    assert settings.llm_base_url == "http://localhost:11434/v1"
    assert settings.llm_model_name == "llama3:70b"
    assert settings.llm_api_key is None
    assert settings.mcp_port == 8003
    assert settings.mcp_log_path == Path("logs/mcp_queries.jsonl")
    assert settings.mcp_log_retention_days == 7
    assert settings.summary_max_concurrency == 3
    assert settings.community_max_calls == 150
    assert settings.relationship_orphan_policy == "log_orphan"
    assert settings.dead_letter_path_orphans == Path("data/dead_letter_orphans.jsonl")


def test_settings_mcp_values_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP settings can be customized via .env values."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "mcp_port": 9000,
        "mcp_log_path": "/var/log/mcp.jsonl",
        "mcp_log_retention_days": 14,
    }
    settings = Settings.model_validate(data)

    assert settings.mcp_port == 9000
    assert settings.mcp_log_path == Path("/var/log/mcp.jsonl")
    assert settings.mcp_log_retention_days == 14


def test_settings_mcp_port_must_be_in_valid_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mcp_port must be a valid TCP port (1-65535)."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "mcp_port": 70000,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "mcp_port" in str(exc_info.value)


def test_settings_mcp_retention_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mcp_log_retention_days must be at least 1."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "mcp_log_retention_days": 0,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "mcp_log_retention_days" in str(exc_info.value)


def test_settings_orphan_policy_rejects_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """relationship_orphan_policy must be 'fail_loud' or 'log_orphan'."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "relationship_orphan_policy": "ignore",
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "relationship_orphan_policy" in str(exc_info.value)


def test_settings_canonical_defaults_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonicalization defaults to deterministic slug mode with empty stoplist."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    settings = Settings.model_validate(data)

    assert settings.canonical_match_mode == "slug"
    assert settings.canonical_fuzzy_threshold == 0.92
    assert settings.canonical_stoplist == []


def test_settings_canonical_fuzzy_threshold_rejects_too_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """canonical_fuzzy_threshold must be >= 0.5."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "canonical_fuzzy_threshold": 0.4,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "canonical_fuzzy_threshold" in str(exc_info.value)


def test_settings_canonical_fuzzy_threshold_rejects_too_high(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """canonical_fuzzy_threshold must be <= 1.0."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "canonical_fuzzy_threshold": 1.1,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "canonical_fuzzy_threshold" in str(exc_info.value)


def test_settings_canonical_stoplist_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """canonical_stoplist accepts a list of domain stopwords."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "canonical_stoplist": ["protocol", "model"],
    }
    settings = Settings.model_validate(data)

    assert settings.canonical_stoplist == ["protocol", "model"]


def test_settings_community_model_name_defaults_to_llm_model_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """community_model_name inherits llm_model_name when not provided."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    settings = Settings.model_validate(data)

    assert settings.community_model_name == settings.llm_model_name
    assert settings.community_model_name == "llama3:70b"


def test_settings_community_model_name_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """community_model_name can be set explicitly."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "community_model_name": "gpt-4.1-mini",
    }
    settings = Settings.model_validate(data)

    assert settings.community_model_name == "gpt-4.1-mini"


def test_settings_max_cluster_size_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_cluster_size defaults to 10."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    settings = Settings.model_validate(data)

    assert settings.max_cluster_size == 10


def test_settings_max_cluster_size_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_cluster_size must be greater than 0."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "max_cluster_size": 0,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "max_cluster_size" in str(exc_info.value)


def test_settings_summary_max_concurrency_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary_max_concurrency defaults to 3."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    settings = Settings.model_validate(data)

    assert settings.summary_max_concurrency == 3


def test_settings_summary_max_concurrency_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary_max_concurrency can be customized."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "summary_max_concurrency": 5,
    }
    settings = Settings.model_validate(data)

    assert settings.summary_max_concurrency == 5


def test_settings_summary_max_concurrency_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary_max_concurrency must be greater than 0."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "summary_max_concurrency": 0,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "summary_max_concurrency" in str(exc_info.value)


def test_settings_community_max_calls_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """community_max_calls defaults to 150."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    settings = Settings.model_validate(data)

    assert settings.community_max_calls == 150


def test_settings_community_max_calls_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """community_max_calls can be customized."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "community_max_calls": 50,
    }
    settings = Settings.model_validate(data)

    assert settings.community_max_calls == 50


def test_settings_community_max_calls_must_be_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """community_max_calls must be greater than 0."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "community_max_calls": 0,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "community_max_calls" in str(exc_info.value)

