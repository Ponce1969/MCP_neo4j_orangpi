"""Unit tests for workspace validation models and rules."""

from __future__ import annotations

from mcp_oranpi.domain.validation import (
    ValidationError,
    validate_workspace_id,
    validate_workspace_path,
)
from mcp_oranpi.domain.workspace import WorkspaceInfo, WorkspaceStatus


class TestWorkspaceStatus:
    """Tests for WorkspaceStatus enum."""

    def test_active_value(self) -> None:
        """WorkspaceStatus.ACTIVE has correct value."""
        assert WorkspaceStatus.ACTIVE == "active"

    def test_disabled_value(self) -> None:
        """WorkspaceStatus.DISABLED has correct value."""
        assert WorkspaceStatus.DISABLED == "disabled"

    def test_unreachable_value(self) -> None:
        """WorkspaceStatus.UNREACHABLE has correct value."""
        assert WorkspaceStatus.UNREACHABLE == "unreachable"

    def test_all_statuses_are_strings(self) -> None:
        """All WorkspaceStatus values are strings."""
        for status in WorkspaceStatus:
            assert isinstance(status.value, str)


class TestWorkspaceInfo:
    """Tests for WorkspaceInfo dataclass."""

    def test_minimal_workspace_info(self) -> None:
        """WorkspaceInfo can be created with required fields."""
        info = WorkspaceInfo(
            id="guardian",
            path="/home/cerra/codigo/guardian",
            status=WorkspaceStatus.ACTIVE,
        )
        assert info.id == "guardian"
        assert info.path == "/home/cerra/codigo/guardian"
        assert info.status == WorkspaceStatus.ACTIVE
        assert info.has_compose is False
        assert info.has_git is False
        assert info.project_type is None
        assert info.detected_services == []
        assert info.docker_compose_project is None
        assert info.last_validated is None

    def test_full_workspace_info(self) -> None:
        """WorkspaceInfo can be created with all fields."""
        info = WorkspaceInfo(
            id="guardian",
            path="/home/cerra/codigo/guardian",
            status=WorkspaceStatus.ACTIVE,
            has_compose=True,
            has_git=True,
            project_type="python",
            detected_services=["postgresql", "redis", "nginx"],
            docker_compose_project="guardian",
            last_validated="2024-01-15T10:30:00Z",
        )
        assert info.has_compose is True
        assert info.has_git is True
        assert info.project_type == "python"
        assert len(info.detected_services) == 3
        assert "postgresql" in info.detected_services
        assert info.docker_compose_project == "guardian"
        assert info.last_validated == "2024-01-15T10:30:00Z"

    def test_disabled_workspace(self) -> None:
        """WorkspaceInfo can represent a disabled workspace."""
        info = WorkspaceInfo(
            id="deleted",
            path="/home/cerra/codigo/deleted",
            status=WorkspaceStatus.DISABLED,
        )
        assert info.status == WorkspaceStatus.DISABLED


class TestWorkspaceValidationFromDomain:
    """Tests for workspace validation using domain validators."""

    def test_valid_id_via_domain(self) -> None:
        """validate_workspace_id works from domain module."""
        assert validate_workspace_id("guardian") == "guardian"

    def test_reject_invalid_id_via_domain(self) -> None:
        """validate_workspace_id rejects via ValidationError."""
        try:
            validate_workspace_id("../../etc")
            raise AssertionError("Expected ValidationError")
        except ValidationError as exc:
            assert exc.code is not None

    def test_valid_path_via_domain(self) -> None:
        """validate_workspace_path works from domain module."""
        result = validate_workspace_path(
            "/home/cerra/codigo/guardian",
            "/home/cerra/codigo",
        )
        assert result == "/home/cerra/codigo/guardian"

    def test_reject_path_escape_via_domain(self) -> None:
        """validate_workspace_path rejects escape via ValidationError."""
        try:
            validate_workspace_path(
                "/home/cerra/codigo/../../etc/passwd",
                "/home/cerra/codigo",
            )
            raise AssertionError("Expected ValidationError")
        except ValidationError as exc:
            assert exc.code is not None