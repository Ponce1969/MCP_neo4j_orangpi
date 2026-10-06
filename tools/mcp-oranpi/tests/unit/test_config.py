"""Unit tests for AppConfig and SSHConfig loading."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from mcp_oranpi.config import AppConfig, ConfigError, OutputLimits, SSHConfig


class TestAppConfig:
    """Tests for AppConfig loading from environment variables."""

    def test_config_from_env(self, mock_env_vars: dict[str, str]) -> None:
        """AppConfig loads successfully with all required env vars."""
        config = AppConfig()  # type: ignore
        assert config.ssh_host == "test-oranpi.local"
        assert config.ssh_user == "testuser"
        assert config.root_workspace_dir == "/home/testuser/codigo"

    def test_config_defaults(self, mock_env_vars: dict[str, str]) -> None:
        """AppConfig fills in default values for optional env vars."""
        config = AppConfig()  # type: ignore
        assert config.ssh_port == 22
        # security_mode and other defaults come from env vars (including .env)
        # so we only test that the fallbacks exist when not overridden
        assert config.ssh_security_mode in ("production", "development")
        assert config.ssh_host_key_policy in ("strict", "accept_new", "ssh_config")
        assert config.ssh_connect_timeout == 10
        assert config.ssh_command_timeout == 30
        assert config.log_level in ("DEBUG", "INFO", "WARNING", "ERROR")
        assert config.log_output_limit_kb == 50
        assert config.max_payload_kb == 1024

    def test_config_missing_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AppConfig raises ValidationError when required vars are missing
        and no .env file provides them."""
        for key in list(os.environ):
            if key.startswith("ORANPI_"):
                monkeypatch.delenv(key, raising=False)
        # Create a config class that doesn't read .env file
        class TestConfig(AppConfig):
            model_config = SettingsConfigDict(
                env_prefix="ORANPI_",
                env_file=None,  # Don't read .env
                extra="ignore",
            )
        with pytest.raises(ValidationError):
            TestConfig()  # type: ignore

    def test_config_missing_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AppConfig raises ValidationError when SSH_HOST is missing."""
        for key in list(os.environ):
            if key.startswith("ORANPI_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ORANPI_SSH_USER", "testuser")
        monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/test/key")
        monkeypatch.setenv("ORANPI_ROOT_WORKSPACE_DIR", "/test/dir")

        class TestConfig(AppConfig):
            model_config = SettingsConfigDict(
                env_prefix="ORANPI_",
                env_file=None,
                extra="ignore",
            )

        with pytest.raises(ValidationError):
            TestConfig()  # type: ignore

    def test_config_missing_workspace_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AppConfig raises ValidationError when ROOT_WORKSPACE_DIR is missing."""
        for key in list(os.environ):
            if key.startswith("ORANPI_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ORANPI_SSH_HOST", "test.local")
        monkeypatch.setenv("ORANPI_SSH_USER", "testuser")
        monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/test/key")
        # Don't set ROOT_WORKSPACE_DIR

        class TestConfig(AppConfig):
            model_config = SettingsConfigDict(
                env_prefix="ORANPI_",
                env_file=None,
                extra="ignore",
            )

        with pytest.raises(ValidationError):
            TestConfig()  # type: ignore

    def test_config_invalid_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AppConfig raises ValidationError when SSH_PORT is not a number."""
        monkeypatch.setenv("ORANPI_SSH_HOST", "test.local")
        monkeypatch.setenv("ORANPI_SSH_USER", "testuser")
        monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/test/key")
        monkeypatch.setenv("ORANPI_ROOT_WORKSPACE_DIR", "/test/dir")
        monkeypatch.setenv("ORANPI_SSH_PORT", "not_a_number")
        with pytest.raises(ValidationError):
            AppConfig()  # type: ignore

    def test_ssh_config_property(self, mock_env_vars: dict[str, str]) -> None:
        """AppConfig.ssh_config derives SSHConfig correctly."""
        config = AppConfig()  # type: ignore
        ssh = config.ssh_config
        assert isinstance(ssh, SSHConfig)
        assert ssh.host == "test-oranpi.local"
        assert ssh.port == 22
        assert ssh.username == "testuser"
        # security_mode comes from env (development in .env) or default (production)
        assert ssh.security_mode in ("production", "development")

    def test_output_limits_property(self, mock_env_vars: dict[str, str]) -> None:
        """AppConfig.output_limits derives OutputLimits correctly."""
        config = AppConfig()  # type: ignore
        limits = config.output_limits
        assert isinstance(limits, OutputLimits)
        assert limits.log_output_limit_kb == 50
        assert limits.max_payload_kb == 1024
        assert limits.max_log_file_mb == 50

    def test_allowed_log_dirs_list(self, mock_env_vars: dict[str, str]) -> None:
        """AppConfig.allowed_log_dirs_list parses comma-separated dirs."""
        config = AppConfig()  # type: ignore
        dirs = config.allowed_log_dirs_list
        assert isinstance(dirs, list)
        # Default value from env var in conftest defaults
        assert len(dirs) >= 1

    def test_custom_log_dirs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AppConfig parses custom allowed_log_dirs."""
        monkeypatch.setenv("ORANPI_SSH_HOST", "test.local")
        monkeypatch.setenv("ORANPI_SSH_USER", "testuser")
        monkeypatch.setenv("ORANPI_SSH_KEY_PATH", "/test/key")
        monkeypatch.setenv("ORANPI_ROOT_WORKSPACE_DIR", "/test/dir")
        monkeypatch.setenv("ORANPI_ALLOWED_LOG_DIRS", "/var/log,/home,/opt,/tmp")
        config = AppConfig()  # type: ignore
        assert config.allowed_log_dirs_list == [
            "/var/log",
            "/home",
            "/opt",
            "/tmp",
        ]


class TestConfigError:
    """Tests for ConfigError exception."""

    def test_config_error_with_field(self) -> None:
        """ConfigError carries message and field."""
        err = ConfigError("Invalid config", field="ssh_host")
        assert err.field == "ssh_host"
        assert str(err) == "Invalid config"

    def test_config_error_without_field(self) -> None:
        """ConfigError field defaults to None."""
        err = ConfigError("Something went wrong")
        assert err.field is None