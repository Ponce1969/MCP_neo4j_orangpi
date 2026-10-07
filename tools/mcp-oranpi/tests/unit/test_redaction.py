"""Tests for the secret redaction module."""

from __future__ import annotations

from mcp_oranpi.domain.redaction import (
    REDACTED,
    _is_sensitive_env_var,
    is_log_path_blocked,
    redact_env_vars,
    redact_env_vars_in_json_dict,
    redact_json_response,
    redact_text,
)


class TestRedactText:
    """Tests for redact_text pattern-based redaction."""

    def test_redacts_private_key(self) -> None:
        text = "config: -----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQE...\n-----END RSA PRIVATE KEY-----"
        result = redact_text(text)
        assert REDACTED in result
        assert "MIIEowIBAAKCAQE" not in result

    def test_redacts_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc123def456="
        result = redact_text(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert REDACTED in result

    def test_redacts_url_credentials(self) -> None:
        text = "Database URL: postgres://admin:secretpassword@db.example.com:5432/mydb"
        result = redact_text(text)
        assert "secretpassword" not in result
        assert REDACTED in result

    def test_redacts_long_hex_tokens(self) -> None:
        # 64-char hex string (like a sha256 hash / JWT secret)
        text = (
            f"JWT_SECRET_KEY={REDACTED}"  # This should NOT be matched since it's already REDACTED
        )
        # Use a real hex token
        secret = "a" * 64
        text = f"token={secret}"
        result = redact_text(text)
        assert secret not in result
        assert REDACTED in result

    def test_does_not_redact_container_ids(self) -> None:
        # 12-char hex (container ID short) should NOT be redacted
        text = "Container b01eec084b05 is running"
        result = redact_text(text)
        assert "b01eec084b05" in result

    def test_does_not_redact_normal_text(self) -> None:
        text = "The container is running on port 8080"
        result = redact_text(text)
        assert result == text

    def test_does_not_redact_empty_string(self) -> None:
        assert redact_text("") == ""

    def test_redacts_cloudflare_tunnel_token(self) -> None:
        text = "tunnel_token=abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        result = redact_text(text)
        assert REDACTED in result
        assert "abcdef1234567890" not in result

    def test_redacts_api_key_prefix(self) -> None:
        text = 'API_KEY="sk-proj-abc123def456ghi789jkl012mno345pqr678"'
        result = redact_text(text)
        assert "sk-proj-abc123" not in result


class TestIsSensitiveEnvVar:
    """Tests for _is_sensitive_env_var."""

    def test_password(self) -> None:
        assert _is_sensitive_env_var("POSTGRES_PASSWORD") is True

    def test_secret(self) -> None:
        assert _is_sensitive_env_var("JWT_SECRET_KEY") is True

    def test_token(self) -> None:
        assert _is_sensitive_env_var("GITHUB_TOKEN") is True

    def test_key(self) -> None:
        assert _is_sensitive_env_var("API_KEY") is True

    def test_client_secret(self) -> None:
        assert _is_sensitive_env_var("MELI_CLIENT_SECRET") is True

    def test_database_url(self) -> None:
        assert _is_sensitive_env_var("DATABASE_URL") is True

    def test_normal_env(self) -> None:
        assert _is_sensitive_env_var("POSTGRES_HOST") is False

    def test_port(self) -> None:
        assert _is_sensitive_env_var("PORT") is False

    def test_host(self) -> None:
        assert _is_sensitive_env_var("HOST") is False

    def test_path(self) -> None:
        assert _is_sensitive_env_var("PATH") is False

    def test_python_version(self) -> None:
        assert _is_sensitive_env_var("PYTHON_VERSION") is False

    def test_case_insensitive(self) -> None:
        assert _is_sensitive_env_var("jwt_Secret_Key") is True

    def test_pass_as_substring(self) -> None:
        # "PASS" in "PASSWORD" is keyword but whole word matches too
        assert _is_sensitive_env_var("PASSWORD") is True

    def test_db_pass(self) -> None:
        assert _is_sensitive_env_var("DB_PASS") is True


class TestRedactEnvVars:
    """Tests for redact_env_vars."""

    def test_redacts_password(self) -> None:
        env = ["POSTGRES_PASSWORD=supersecret123", "PORT=5432"]
        result = redact_env_vars(env)
        assert result[0] == f"POSTGRES_PASSWORD={REDACTED}"
        assert result[1] == "PORT=5432"

    def test_redacts_secret_key(self) -> None:
        env = ["JWT_SECRET_KEY=abc123def456", "HOST=0.0.0.0"]
        result = redact_env_vars(env)
        assert result[0] == f"JWT_SECRET_KEY={REDACTED}"
        assert result[1] == "HOST=0.0.0.0"

    def test_preserves_non_secret(self) -> None:
        env = ["PORT=8000", "HOST=0.0.0.0", "DEBUG=false"]
        result = redact_env_vars(env)
        assert result == env

    def test_empty_entry(self) -> None:
        env = ["NOEQUALSSIGN"]
        result = redact_env_vars(env)
        assert result == ["NOEQUALSSIGN"]

    def test_meli_client_secret(self) -> None:
        env = ["MELI_CLIENT_SECRET=drj6DLOrSB4d0HqE0Rj82DmNS4vfJYgL"]
        result = redact_env_vars(env)
        assert "drj6DLOrSB4d0HqE0Rj82DmNS4vfJYgL" not in str(result)
        assert REDACTED in result[0]

    def test_demo_pass(self) -> None:
        env = ["DEMO_PASS=tmo2025"]
        result = redact_env_vars(env)
        assert result[0] == f"DEMO_PASS={REDACTED}"


class TestRedactEnvVarsInJsonDict:
    """Tests for redact_env_vars_in_json_dict."""

    def test_redacts_dict(self) -> None:
        data = {"POSTGRES_PASSWORD": "abc123", "PORT": "5432"}
        result = redact_env_vars_in_json_dict(data)
        assert result["POSTGRES_PASSWORD"] == REDACTED
        assert result["PORT"] == "5432"

    def test_preserves_non_secret(self) -> None:
        data = {"HOST": "0.0.0.0", "PORT": "8000"}
        result = redact_env_vars_in_json_dict(data)
        assert result == data


class TestIsLogPathBlocked:
    """Tests for is_log_path_blocked."""

    def test_blocks_cloudflared_dir(self) -> None:
        blocked, reason = is_log_path_blocked("/home/gonzalo/.cloudflared/config.yml")
        assert blocked is True
        assert "cloudflared" in reason.lower()

    def test_blocks_ssh_dir(self) -> None:
        blocked, reason = is_log_path_blocked("/home/gonzalo/.ssh/id_rsa")
        assert blocked is True
        assert "ssh" in reason.lower()

    def test_blocks_env_file(self) -> None:
        blocked, reason = is_log_path_blocked("/home/gonzalo/app/.env")
        assert blocked is True

    def test_blocks_pem_file(self) -> None:
        blocked, reason = is_log_path_blocked("/etc/ssl/certs/server.pem")
        assert blocked is True
        assert ".pem" in reason.lower()

    def test_blocks_key_file(self) -> None:
        blocked, reason = is_log_path_blocked("/home/user/server.key")
        assert blocked is True
        assert ".key" in reason.lower()

    def test_blocks_aws_dir(self) -> None:
        blocked, reason = is_log_path_blocked("/home/user/.aws/credentials")
        assert blocked is True

    def test_allows_var_log(self) -> None:
        blocked, reason = is_log_path_blocked("/var/log/syslog")
        assert blocked is False
        assert reason == ""

    def test_allows_docker_logs(self) -> None:
        blocked, reason = is_log_path_blocked("/var/log/docker.log")
        assert blocked is False

    def test_blocks_shadow(self) -> None:
        blocked, reason = is_log_path_blocked("/etc/shadow")
        assert blocked is True

    def test_blocks_htpasswd(self) -> None:
        blocked, reason = is_log_path_blocked("/etc/nginx/.htpasswd")
        assert blocked is True

    def test_blocks_cloudflare_credential_json(self) -> None:
        blocked, reason = is_log_path_blocked(
            "/home/gonzalo/.cloudflared/19f0b567-aae3-4e2c-9137-4ed1ffb325ac.json"
        )
        assert blocked is True

    def test_blocks_gnupg(self) -> None:
        blocked, reason = is_log_path_blocked("/home/user/.gnupg/secring.gpg")
        assert blocked is True


class TestRedactJsonResponse:
    """Tests for redact_json_response (recursive JSON redaction)."""

    def test_redacts_env_list_in_docker_inspect(self) -> None:
        """Simulates the exact docker_inspect output with env vars."""
        data = {
            "id": "b01eec084b05",
            "name": "svl-app",
            "env": [
                "POSTGRES_PASSWORD=5iycuxnRUzZ1bmB8GRrr1MHD",
                "MELI_CLIENT_SECRET=drj6DLOrSB4d0HqE0Rj82DmNS4vfJYgL",
                "JWT_SECRET_KEY=42fc12c351c52d8d3a9fe80a0c05cfb7",
                "DEMO_PASS=tmo2025",
                "PORT=8000",
                "HOST=0.0.0.0",
            ],
        }
        result = redact_json_response(data)  # type: ignore
        assert result["env"][0] == f"POSTGRES_PASSWORD={REDACTED}"  # type: ignore
        assert result["env"][1] == f"MELI_CLIENT_SECRET={REDACTED}"  # type: ignore
        assert result["env"][2] == f"JWT_SECRET_KEY={REDACTED}"  # type: ignore
        assert result["env"][3] == f"DEMO_PASS={REDACTED}"  # type: ignore
        assert result["env"][4] == "PORT=8000"  # type: ignore
        assert result["env"][5] == "HOST=0.0.0.0"  # type: ignore

    def test_redacts_env_dict(self) -> None:
        data = {"env": {"POSTGRES_PASSWORD": "abc123", "PORT": "5432"}}
        result = redact_json_response(data)  # type: ignore
        assert result["env"]["POSTGRES_PASSWORD"] == REDACTED  # type: ignore
        assert result["env"]["PORT"] == "5432"  # type: ignore

    def test_redacts_nested_text(self) -> None:
        data = {"config": {"url": "postgres://admin:secretpassword@db:5432/mydb"}}
        result = redact_json_response(data)  # type: ignore
        assert "secretpassword" not in str(result)

    def test_preserves_non_secret_data(self) -> None:
        data = {"containers": [{"name": "svl-app", "status": "running", "port": 8002}]}
        result = redact_json_response(data)  # type: ignore
        assert result == data

    def test_handles_none(self) -> None:
        assert redact_json_response(None) is None

    def test_handles_numbers(self) -> None:
        assert redact_json_response(42) == 42
        assert redact_json_response(3.14) == 3.14

    def test_handles_bool(self) -> None:
        assert redact_json_response(True) is True

    def test_redacts_strings(self) -> None:
        result = redact_json_response("Bearer abc123def456=")
        assert "abc123" not in str(result)

    def test_preserves_non_env_list(self) -> None:
        data = {"ports": ["8000/tcp", "5432/tcp"]}
        result = redact_json_response(data)  # type: ignore
        assert result == data
