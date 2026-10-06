"""Workspace domain models and validation for MCP OranPi.

This module contains ONLY pure domain logic: data models, status definitions,
and validation rules. It has NO knowledge of SSH, filesystem operations,
or remote host access.

The actual resolution of workspace paths (checking if directories exist on
the OrangePi, resolving symlinks over SSH) lives in
``infrastructure/workspace_resolver.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WorkspaceStatus(StrEnum):
    """Status of a workspace after validation.

    - ACTIVE: directory exists on the remote host and is under the root
    - DISABLED: directory does not exist or path validation failed
    - UNREACHABLE: SSH connection failed during validation
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    UNREACHABLE = "unreachable"


@dataclass(slots=True)
class WorkspaceInfo:
    """Metadata about a registered workspace.

    Returned by the ``workspace_list`` and ``workspace_inspect`` tools.
    """

    id: str
    path: str
    status: WorkspaceStatus
    has_compose: bool = False
    has_git: bool = False
    project_type: str | None = None
    detected_services: list[str] = field(default_factory=list)
    docker_compose_project: str | None = None
    last_validated: str | None = None


# ── Validation Patterns ──────────────────────────────────────────────────────

# Workspace identifiers must match this pattern.
# Allows letters, digits, underscores, and hyphens.
# Rejects path traversal, spaces, and special characters.
WORKSPACE_ID_PATTERN: str = r"^[a-zA-Z0-9_-]+$"

# Container names must match Docker's naming rules.
CONTAINER_NAME_PATTERN: str = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$"

# Service names (systemd unit names).
SERVICE_NAME_PATTERN: str = r"^[a-zA-Z0-9_.-]+$"
