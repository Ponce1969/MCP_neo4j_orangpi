"""Rules for selecting the optional read-only Neo4j credential."""

from __future__ import annotations

from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import is_effective_secret_shape


def _readonly_credentials(settings: Settings) -> tuple[str, str] | None:
    """Return trimmed read credentials only when both are real, printable values.

    The shape check is shared with the token rule: ``str.strip`` does not remove invisible
    non-whitespace characters, so a password of only those would look configured and production
    would boot with a credential that cannot authenticate — the failure would surface at query
    time instead of at startup. The length floor deliberately does **not** apply: a database
    password is not a bearer token (the host's is 15 characters).
    """
    # Test doubles and legacy callers may expose only the required Settings subset; absent
    # optional fields have the same meaning as their model defaults.
    user = getattr(settings, "neo4j_readonly_user", "")
    password_secret = getattr(settings, "neo4j_readonly_password", None)
    if password_secret is None:
        return None

    password = password_secret.get_secret_value()
    if not is_effective_secret_shape(user) or not is_effective_secret_shape(password):
        return None
    return user.strip(), password.strip()


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
