"""Compute the checkpoint VersionDimensions from PDF bytes and Settings."""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import VersionDimensions


def _provider_name(settings: Settings) -> str:
    """Derive a short provider slug from the graph LLM base URL."""
    base_url = settings.graph_llm_base_url or ""
    if not base_url.strip():
        return "openai"
    hostname = urlparse(base_url).hostname or ""
    hostname_lower = hostname.lower()
    if "openai" in hostname_lower:
        return "openai"
    if "ollama" in hostname_lower:
        return "ollama"
    parts = hostname_lower.split(".")
    if parts and parts[0]:
        return parts[0]
    return "custom"


def compute_version_dimensions(pdf_bytes: bytes, settings: Settings) -> VersionDimensions:
    """Return the four version dimensions that identify a checkpoint run.

    * source_version  - sha256(pdf_bytes)[:16]
    * pipeline_version - configured pipeline semver
    * model_version    - provider:model:date
    * schema_version   - configured schema semver
    """
    return VersionDimensions(
        source_version=hashlib.sha256(pdf_bytes).hexdigest()[:16],
        pipeline_version=settings.pipeline_version,
        model_version=(
            f"{_provider_name(settings)}:"
            f"{settings.graph_llm_model_name}:"
            f"{settings.graph_llm_model_date}"
        ),
        schema_version=settings.schema_version,
    )
