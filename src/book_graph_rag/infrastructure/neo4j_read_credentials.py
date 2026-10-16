"""Rules for selecting the optional read-only Neo4j credential."""

from __future__ import annotations

from pydantic import SecretStr

from book_graph_rag.config import Settings


def _readonly_credentials(settings: Settings) -> tuple[str, str] | None:
    """Return trimmed read credentials only when both are non-empty."""
    # Test doubles and legacy callers may expose only the required Settings subset; absent
    # optional fields have the same meaning as their model defaults.
    user = getattr(settings, "neo4j_readonly_user", "").strip()
    password_secret = getattr(settings, "neo4j_readonly_password", None)
    if password_secret is None:
        return None

    password = password_secret.get_secret_value().strip()
    if not user or not password:
        return None
    return user, password


def has_readonly_credentials(settings: Settings) -> bool:
    """Whether both read-only Neo4j credential values are configured."""
    return _readonly_credentials(settings) is not None


def readonly_settings(settings: Settings) -> Settings:
    """Return a settings view using the read credential, or the original settings as fallback."""
    credentials = _readonly_credentials(settings)
    if credentials is None:
        return settings

    user, password = credentials
    return settings.model_copy(update={"neo4j_user": user, "neo4j_password": SecretStr(password)})
