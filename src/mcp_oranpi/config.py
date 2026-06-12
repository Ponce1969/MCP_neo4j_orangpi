"""Typed configuration for MCP OranPi.

Loads all settings from environment variables using pydantic-settings.
This module is the single source of truth for runtime configuration.

Domain-level validation functions live in ``domain/validation.py``.
This module only contains configuration loading and derived properties.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class SSHConfig(BaseModel):
    """SSH connection configuration.

    Loaded from ORANPI_SSH_* environment variables.
    """

    host: str
    port: int = 22
    username: str
    key_path: Path
    known_hosts: Path | None = None
    security_mode: Literal["production", "development"] = "production"
    host_key_policy: Literal["strict", "accept_new", "ssh_config"] = "strict"
    connect_timeout: int = 10
    keepalive_interval: int = 30
    command_timeout: int = 30


class WorkspaceConfig(BaseModel):
    """A single registered workspace.

    Maps a logical identifier to a path on the remote OrangePi.
    """

    id: str
    path: str


class OutputLimits(BaseModel):
    """Output truncation and bounding configuration."""

    log_output_limit_kb: int = 50
    max_payload_kb: int = 1024
    max_log_file_mb: int = 50
    max_list_length: int = 500
    max_single_field_kb: int = 100


class AppConfig(BaseSettings):
    """Top-level application configuration.

    Loads from ORANPI_* environment variables.
    """

    model_config = SettingsConfigDict(
        env_prefix="ORANPI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # SSH connection
    ssh_host: str
    ssh_user: str
    ssh_key_path: Path
    ssh_port: int = 22
    ssh_known_hosts: Path | None = None
    ssh_host_key_policy: Literal["strict", "accept_new", "ssh_config"] = "strict"
    ssh_security_mode: Literal["production", "development"] = "production"
    ssh_connect_timeout: int = 10
    ssh_command_timeout: int = 30

    # Workspace
    root_workspace_dir: str
    workspace_config: str = "workspaces.yaml"

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Output limits
    allowed_log_dirs: str = "/var/log,/home,/opt"
    log_output_limit_kb: int = 50
    max_payload_kb: int = 1024
    max_log_file_mb: int = 50

    @property
    def ssh_config(self) -> SSHConfig:
        """Derive SSHConfig from flat env vars."""
        return SSHConfig(
            host=self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
            key_path=self.ssh_key_path,
            known_hosts=self.ssh_known_hosts,
            security_mode=self.ssh_security_mode,
            host_key_policy=self.ssh_host_key_policy,
            connect_timeout=self.ssh_connect_timeout,
            command_timeout=self.ssh_command_timeout,
        )

    @property
    def output_limits(self) -> OutputLimits:
        """Derive output limits from flat env vars."""
        return OutputLimits(
            log_output_limit_kb=self.log_output_limit_kb,
            max_payload_kb=self.max_payload_kb,
            max_log_file_mb=self.max_log_file_mb,
        )

    @property
    def allowed_log_dirs_list(self) -> list[str]:
        """Parse comma-separated allowed_log_dirs into a list."""
        return [d.strip() for d in self.allowed_log_dirs.split(",") if d.strip()]


class ConfigError(Exception):
    """Configuration validation error."""

    def __init__(self, message: str, field: str | None = None) -> None:
        self.field = field
        super().__init__(message)