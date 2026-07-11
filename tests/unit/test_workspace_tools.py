"""Tests for WorkspaceTools in application/workspace_tools.py.

Uses MockCommandRunner and MockWorkspaceResolver to test workspace tools.
"""

from __future__ import annotations

import pytest

from mcp_oranpi.application.workspace_tools import WorkspaceTools
from mcp_oranpi.domain.errors import (
    DOCKER_NOT_FOUND,
    WS_DISABLED,
    WS_NO_COMPOSE,
    WS_NOT_FOUND,
)
from mcp_oranpi.domain.workspace import WorkspaceInfo, WorkspaceStatus
from mcp_oranpi.infrastructure.ssh_client import CommandResult
from tests.helpers import MockCommandRunner, MockWorkspaceResolver


@pytest.fixture
def mock_runner() -> MockCommandRunner:
    """Provide a MockCommandRunner for WorkspaceTools tests."""
    return MockCommandRunner()


@pytest.fixture
def mock_resolver() -> MockWorkspaceResolver:
    """Provide a MockWorkspaceResolver for WorkspaceTools tests."""
    return MockWorkspaceResolver()


@pytest.fixture
def workspace_tools(
    mock_runner: MockCommandRunner, mock_resolver: MockWorkspaceResolver
) -> WorkspaceTools:
    """Provide a WorkspaceTools instance with mocks."""
    return WorkspaceTools(mock_runner, mock_resolver)


@pytest.fixture
def active_workspace() -> WorkspaceInfo:
    """Provide an active workspace for testing."""
    return WorkspaceInfo(
        id="guardian",
        path="/home/testuser/codigo/guardian",
        status=WorkspaceStatus.ACTIVE,
        has_compose=True,
        has_git=True,
        project_type="docker-compose",
        detected_services=["web", "db"],
        docker_compose_project="guardian",
    )


class TestWorkspaceList:
    """Tests for workspace_list."""

    async def test_success(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "guardian": active_workspace,
                "crm": WorkspaceInfo(
                    id="crm",
                    path="/home/testuser/codigo/crm",
                    status=WorkspaceStatus.ACTIVE,
                    has_compose=True,
                ),
            }
        )

        result = await workspace_tools.workspace_list()

        assert result.error is None
        assert result.data is not None
        assert result.data["total_count"] == 2
        assert len(result.data["workspaces"]) == 2

    async def test_empty(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces({})

        result = await workspace_tools.workspace_list()

        assert result.error is None
        assert result.data is not None
        assert result.data["total_count"] == 0


class TestWorkspaceInspect:
    """Tests for workspace_inspect."""

    async def test_success(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces({"guardian": active_workspace})

        result = await workspace_tools.workspace_inspect("guardian")

        assert result.error is None
        assert result.data is not None
        assert result.data["id"] == "guardian"
        assert result.data["status"] == "active"

    async def test_not_found(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces({})

        result = await workspace_tools.workspace_inspect("nonexistent")

        assert result.error is not None
        assert result.error.code == WS_NOT_FOUND

    async def test_disabled(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "disabled_ws": WorkspaceInfo(
                    id="disabled_ws",
                    path="/home/testuser/codigo/disabled",
                    status=WorkspaceStatus.DISABLED,
                )
            }
        )

        result = await workspace_tools.workspace_inspect("disabled_ws")

        assert result.error is not None
        assert result.error.code == WS_DISABLED

    async def test_unreachable(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "unreachable_ws": WorkspaceInfo(
                    id="unreachable_ws",
                    path="/home/testuser/codigo/unreachable",
                    status=WorkspaceStatus.UNREACHABLE,
                )
            }
        )

        result = await workspace_tools.workspace_inspect("unreachable_ws")

        assert result.error is not None
        assert result.error.code == WS_DISABLED

    async def test_invalid_workspace_id(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        result = await workspace_tools.workspace_inspect("invalid id")

        assert result.error is not None
        assert result.error.code == "VALID_PARAM_INVALID"


class TestWorkspaceDockerPs:
    """Tests for workspace_docker_ps."""

    async def test_success(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        mock_runner: MockCommandRunner,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces({"guardian": active_workspace})
        mock_runner.set_response(
            "compose_ps",
            CommandResult(
                exit_code=0,
                stdout='{"Name":"guardian-web-1","Service":"web","State":"running","Health":"","Publishers":[{"PublishedPort":8080,"TargetPort":80,"Protocol":"tcp","BindIP":"0.0.0.0"}]}',
                stderr="",
            ),
        )

        result = await workspace_tools.workspace_docker_ps("guardian")

        assert result.error is None
        assert result.data is not None
        assert result.data["workspace"] == "guardian"
        assert len(result.data["containers"]) == 1

    async def test_workspace_not_found(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces({})

        result = await workspace_tools.workspace_docker_ps("nonexistent")

        assert result.error is not None
        assert result.error.code == WS_NOT_FOUND

    async def test_disabled_workspace(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "disabled_ws": WorkspaceInfo(
                    id="disabled_ws",
                    path="/home/testuser/codigo/disabled",
                    status=WorkspaceStatus.DISABLED,
                    has_compose=True,
                )
            }
        )

        result = await workspace_tools.workspace_docker_ps("disabled_ws")

        assert result.error is not None
        assert result.error.code == WS_DISABLED

    async def test_no_compose(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "no_compose": WorkspaceInfo(
                    id="no_compose",
                    path="/home/testuser/codigo/no_compose",
                    status=WorkspaceStatus.ACTIVE,
                    has_compose=False,
                )
            }
        )

        result = await workspace_tools.workspace_docker_ps("no_compose")

        assert result.error is not None
        assert result.error.code == WS_NO_COMPOSE


class TestWorkspaceLogs:
    """Tests for workspace_logs."""

    async def test_success(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        mock_runner: MockCommandRunner,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces({"guardian": active_workspace})
        mock_runner.set_response(
            "compose_logs",
            CommandResult(
                exit_code=0,
                stdout="2024-01-15 10:30:00 Web server started\n2024-01-15 10:30:01 Processing request",
                stderr="",
            ),
        )

        result = await workspace_tools.workspace_logs("guardian", tail=100)

        assert result.error is None
        assert result.data is not None
        assert result.data["workspace"] == "guardian"
        assert "Web server started" in result.data["logs"]

    async def test_with_service(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        mock_runner: MockCommandRunner,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces({"guardian": active_workspace})
        mock_runner.set_response(
            "compose_logs",
            CommandResult(exit_code=0, stdout="db logs here", stderr=""),
        )

        result = await workspace_tools.workspace_logs("guardian", service="db", tail=50)

        assert result.error is None
        assert result.data is not None
        assert result.data["service"] == "db"
        last_call = mock_runner.last_call()
        assert last_call is not None
        assert "service" in last_call[1]

    async def test_workspace_not_found(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces({})

        result = await workspace_tools.workspace_logs("nonexistent")

        assert result.error is not None
        assert result.error.code == WS_NOT_FOUND

    async def test_service_not_found(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        mock_runner: MockCommandRunner,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces({"guardian": active_workspace})
        mock_runner.set_response(
            "compose_logs",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="ERROR: No such service: nonexistent",
            ),
        )

        result = await workspace_tools.workspace_logs(
            "guardian", service="nonexistent"
        )

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_FOUND

    async def test_disabled_workspace(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "disabled_ws": WorkspaceInfo(
                    id="disabled_ws",
                    path="/home/testuser/codigo/disabled",
                    status=WorkspaceStatus.DISABLED,
                    has_compose=True,
                )
            }
        )

        result = await workspace_tools.workspace_logs("disabled_ws")

        assert result.error is not None
        assert result.error.code == WS_DISABLED


class TestWorkspacePorts:
    """Tests for workspace_ports."""

    async def test_success(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        mock_runner: MockCommandRunner,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces({"guardian": active_workspace})
        mock_runner.set_response(
            "compose_ps",
            CommandResult(
                exit_code=0,
                stdout='{"Name":"guardian-web-1","Service":"web","State":"running","Health":"","Publishers":[{"PublishedPort":8080,"TargetPort":80,"Protocol":"tcp","BindIP":"0.0.0.0"}]}',
                stderr="",
            ),
        )

        result = await workspace_tools.workspace_ports("guardian")

        assert result.error is None
        assert result.data is not None
        assert result.data["workspace"] == "guardian"
        assert len(result.data["ports"]) == 1
        assert result.data["ports"][0]["host_port"] == 8080

    async def test_workspace_not_found(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces({})

        result = await workspace_tools.workspace_ports("nonexistent")

        assert result.error is not None
        assert result.error.code == WS_NOT_FOUND

    async def test_disabled_workspace(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "disabled_ws": WorkspaceInfo(
                    id="disabled_ws",
                    path="/home/testuser/codigo/disabled",
                    status=WorkspaceStatus.DISABLED,
                    has_compose=True,
                )
            }
        )

        result = await workspace_tools.workspace_ports("disabled_ws")

        assert result.error is not None
        assert result.error.code == WS_DISABLED

    async def test_no_compose(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        mock_runner: MockCommandRunner,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "no_compose": WorkspaceInfo(
                    id="no_compose",
                    path="/home/testuser/codigo/no_compose",
                    status=WorkspaceStatus.ACTIVE,
                    has_compose=False,
                )
            }
        )
        # Simulate compose_ps failing when no compose file exists
        mock_runner.set_response(
            "compose_ps",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="no such file, RERUNNING: docker compose ps",
            ),
        )

        result = await workspace_tools.workspace_ports("no_compose")

        assert result.error is not None
        assert result.error.code == WS_NO_COMPOSE


class TestWorkspaceDeploy:
    """Tests for workspace_deploy (read-only deployment plan)."""

    async def test_returns_manual_plan(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        active_workspace: WorkspaceInfo,
    ) -> None:
        mock_resolver.set_workspaces({"guardian": active_workspace})

        result = await workspace_tools.workspace_deploy("guardian")

        assert result.error is None
        assert result.data is not None
        assert result.data["workspace"] == "guardian"
        assert result.data["status"] == "manual_action_required"
        assert result.data["executed"] is False
        assert result.data["safe_for_agent"] is True
        assert "git pull origin main" in "\n".join(result.data["steps"])
        assert "docker compose" in "\n".join(result.data["steps"])
        # No SSH command should have been executed
        assert "deploy_project" not in result.data["steps"]

    async def test_workspace_not_found(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces({})

        result = await workspace_tools.workspace_deploy("nonexistent")

        assert result.error is not None
        assert result.error.code == WS_NOT_FOUND

    async def test_disabled_workspace(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
    ) -> None:
        mock_resolver.set_workspaces(
            {
                "disabled_ws": WorkspaceInfo(
                    id="disabled_ws",
                    path="/home/testuser/codigo/disabled",
                    status=WorkspaceStatus.DISABLED,
                )
            }
        )

        result = await workspace_tools.workspace_deploy("disabled_ws")

        assert result.error is not None
        assert result.error.code == WS_DISABLED

    async def test_no_destructive_command_executed(
        self,
        workspace_tools: WorkspaceTools,
        mock_resolver: MockWorkspaceResolver,
        mock_runner: MockCommandRunner,
        active_workspace: WorkspaceInfo,
    ) -> None:
        """Ensure workspace_deploy never calls CommandRunner.run()."""
        mock_resolver.set_workspaces({"guardian": active_workspace})
        mock_runner.set_response(
            "deploy_project",
            CommandResult(exit_code=0, stdout="SHOULD NOT BE CALLED", stderr=""),
        )

        result = await workspace_tools.workspace_deploy("guardian")

        assert result.error is None
        assert result.data["status"] == "manual_action_required"
        # The mock response should not be used
        assert "SHOULD NOT BE CALLED" not in str(result.data)