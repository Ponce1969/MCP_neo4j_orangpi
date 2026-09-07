"""Tests for book_graph_rag.config.Settings."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from book_graph_rag.config import Settings, validate_llm_provider_settings


def _clear_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove required Neo4j env vars so tests are independent of the shell."""
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


def test_settings_fails_fast_without_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-01.2: Settings() must fail fast when no .env or env vars are present."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings.model_validate({})


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
    assert settings.graph_llm_base_url == ""
    assert settings.graph_llm_model_name == ""
    assert settings.graph_llm_api_key is None
    assert settings.query_llm_base_url == ""
    assert settings.query_llm_model_name == ""
    assert settings.query_llm_api_key is None
    assert settings.mcp_port == 8003
    assert settings.mcp_log_path == Path("logs/mcp_queries.jsonl")
    assert settings.mcp_log_retention_days == 7
    assert settings.summary_max_concurrency == 3
    assert settings.community_max_calls == 150
    assert settings.relationship_orphan_policy == "log_orphan"
    assert settings.dead_letter_path_orphans == Path("data/dead_letter_orphans.jsonl")
    assert settings.catalog_path == Path("catalog.yaml")


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_settings_role_specific_llm_values_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph and query LLM settings remain independently configurable."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "graph_llm_api_key": "graph-secret",
        "graph_llm_base_url": "https://graph-provider.test/v1",
        "graph_llm_model_name": "graph-model",
        "query_llm_api_key": "query-secret",
        "query_llm_base_url": "https://query-provider.test/v1",
        "query_llm_model_name": "query-model",
    }
    settings = Settings.model_validate(data)

    assert settings.graph_llm_api_key is not None
    assert settings.graph_llm_api_key.get_secret_value() == "graph-secret"
    assert settings.graph_llm_base_url == "https://graph-provider.test/v1"
    assert settings.graph_llm_model_name == "graph-model"
    assert settings.query_llm_api_key is not None
    assert settings.query_llm_api_key.get_secret_value() == "query-secret"
    assert settings.query_llm_base_url == "https://query-provider.test/v1"
    assert settings.query_llm_model_name == "query-model"


def test_settings_loads_role_llm_values_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role-specific settings map to their exact environment variable names."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("GRAPH_LLM_API_KEY", "graph-secret")
    monkeypatch.setenv("GRAPH_LLM_BASE_URL", "https://graph-provider.test/v1")
    monkeypatch.setenv("GRAPH_LLM_MODEL_NAME", "graph-model")
    monkeypatch.setenv("QUERY_LLM_API_KEY", "query-secret")
    monkeypatch.setenv("QUERY_LLM_BASE_URL", "https://query-provider.test/v1")
    monkeypatch.setenv("QUERY_LLM_MODEL_NAME", "query-model")

    settings = Settings(
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("secret"),
    )

    assert settings.graph_llm_api_key is not None
    assert settings.graph_llm_api_key.get_secret_value() == "graph-secret"
    assert settings.graph_llm_base_url == "https://graph-provider.test/v1"
    assert settings.graph_llm_model_name == "graph-model"
    assert settings.query_llm_api_key is not None
    assert settings.query_llm_api_key.get_secret_value() == "query-secret"
    assert settings.query_llm_base_url == "https://query-provider.test/v1"
    assert settings.query_llm_model_name == "query-model"


def test_validate_llm_provider_settings_requires_selected_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider URL and model are required per selected role, not API key."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "query_llm_base_url": "https://query-provider.test/v1",
            "query_llm_model_name": "query-model",
        }
    )

    validate_llm_provider_settings(settings, roles=("query",))

    with pytest.raises(ValueError, match="GRAPH_LLM_BASE_URL.*GRAPH_LLM_MODEL_NAME"):
        validate_llm_provider_settings(settings, roles=("graph",))


def test_settings_role_llm_values_can_be_configured_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph and query roles can use different providers and models."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "graph_llm_base_url": "https://graph-provider.test/v1",
        "graph_llm_model_name": "graph-model",
        "query_llm_base_url": "https://query-provider.test/v1",
        "query_llm_model_name": "query-model",
    }
    settings = Settings.model_validate(data)

    assert settings.graph_llm_model_name != settings.query_llm_model_name
    assert settings.graph_llm_base_url != settings.query_llm_base_url


def test_settings_max_cluster_size_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

# ── Phase 3: Semantic Entity Resolution settings validators ──────────────────


_APPROVED_MODELS = (
    "paraphrase-multilingual-MiniLM-L12-v2",
    "distiluse-base-multilingual-cased-v2",
    "all-MiniLM-L6-v2",
)


def test_settings_resolution_defaults_are_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default retrieval strategy is brute_force and model is approved."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
    }
    settings = Settings.model_validate(data)

    assert settings.candidate_retrieval_strategy == "brute_force"
    assert settings.embedding_model_id in _APPROVED_MODELS
    assert settings.embedding_input_variant == "A"
    assert settings.embedding_top_k == 20
    assert settings.embedding_min_similarity == pytest.approx(0.60)
    assert settings.vector_index_name == "entity_embedding_index"


def test_settings_embedding_model_id_must_be_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown embedding_model_id is rejected unless allow_extra_embedding_model is True."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "embedding_model_id": "unknown-model",
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "embedding_model_id" in str(exc_info.value)


def test_settings_allow_extra_embedding_model_permits_unknown_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allow_extra_embedding_model=True bypasses the approved-model list."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "embedding_model_id": "unknown-model",
        "allow_extra_embedding_model": True,
    }
    settings = Settings.model_validate(data)

    assert settings.embedding_model_id == "unknown-model"


@pytest.mark.parametrize("top_k", [0, 101, -5])
def test_settings_embedding_top_k_out_of_range_rejected(
    top_k: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """embedding_top_k must be between 1 and 100 inclusive."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "embedding_top_k": top_k,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "embedding_top_k" in str(exc_info.value)


@pytest.mark.parametrize("min_sim", [-0.1, 1.1, 2.0])
def test_settings_embedding_min_similarity_out_of_range_rejected(
    min_sim: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """embedding_min_similarity must be in [0.0, 1.0]."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "embedding_min_similarity": min_sim,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "embedding_min_similarity" in str(exc_info.value)


@pytest.mark.parametrize(
    "threshold",
    [
        "band_high_cosine",
        "band_high_context",
        "band_medium_cosine",
        "band_conflict_floor",
    ],
)
@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_settings_band_thresholds_out_of_range_rejected(
    threshold: str, value: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Band thresholds must be within [0.0, 1.0]."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        threshold: value,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert threshold in str(exc_info.value)


def test_settings_band_high_cosine_must_be_greater_than_medium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """band_high_cosine must be strictly greater than band_medium_cosine."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "band_high_cosine": 0.80,
        "band_medium_cosine": 0.80,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "band_high_cosine" in str(exc_info.value)
    assert "band_medium_cosine" in str(exc_info.value)


def test_settings_neo4j_vector_requires_embedding_dim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """candidate_retrieval_strategy=neo4j_vector requires embedding_dim to be set."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "candidate_retrieval_strategy": "neo4j_vector",
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "embedding_dim" in str(exc_info.value)


def test_settings_neo4j_vector_accepts_embedding_dim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """candidate_retrieval_strategy=neo4j_vector is valid when embedding_dim is provided."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "candidate_retrieval_strategy": "neo4j_vector",
        "embedding_dim": 384,
    }
    settings = Settings.model_validate(data)

    assert settings.candidate_retrieval_strategy == "neo4j_vector"
    assert settings.embedding_dim == 384


def test_settings_embedding_input_variant_rejects_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """embedding_input_variant must be 'A' or 'B'."""
    monkeypatch.chdir(tmp_path)
    _clear_required_env(monkeypatch)

    data = {
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "embedding_input_variant": "C",
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(data)

    assert "embedding_input_variant" in str(exc_info.value)

