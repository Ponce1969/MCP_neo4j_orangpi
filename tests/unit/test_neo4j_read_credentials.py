import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_read_credentials import (
    has_readonly_credentials,
    readonly_settings,
)


def _settings(*, readonly_user: str = "", readonly_password: SecretStr | None = None) -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "writer",
            "neo4j_password": "writer-password",
            "neo4j_readonly_user": readonly_user,
            "neo4j_readonly_password": readonly_password,
        }
    )


def test_settings_exposes_optional_readonly_credential_fields() -> None:
    settings = _settings()

    assert settings.neo4j_readonly_user == ""
    assert settings.neo4j_readonly_password is None


@pytest.mark.parametrize(
    ("readonly_user", "readonly_password", "expected"),
    [
        ("reader", SecretStr("short-pass"), True),
        ("  reader  ", SecretStr("  short-pass  "), True),
        ("", SecretStr("short-pass"), False),
        ("reader", None, False),
        ("   ", SecretStr("short-pass"), False),
        ("reader", SecretStr(" \t\n "), False),
    ],
)
def test_has_readonly_credentials_requires_both_nonblank_values(
    readonly_user: str,
    readonly_password: SecretStr | None,
    expected: bool,
) -> None:
    assert (
        has_readonly_credentials(
            _settings(readonly_user=readonly_user, readonly_password=readonly_password)
        )
        is expected
    )


def test_readonly_settings_trims_both_credentials_without_a_token_length_floor() -> None:
    settings = _settings(readonly_user="  reader  ", readonly_password=SecretStr("  short-pass  "))

    read_view = readonly_settings(settings)

    assert read_view.neo4j_user == "reader"
    assert read_view.neo4j_password.get_secret_value() == "short-pass"
    assert read_view.neo4j_uri == settings.neo4j_uri
    assert settings.neo4j_user == "writer"
    assert settings.neo4j_password.get_secret_value() == "writer-password"


def test_readonly_settings_falls_back_to_original_settings_when_unconfigured() -> None:
    settings = _settings(readonly_user="reader", readonly_password=None)

    assert readonly_settings(settings) is settings
