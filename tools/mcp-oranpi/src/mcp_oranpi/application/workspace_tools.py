"""Workspace audit tools for MCP OranPi.

Provides read-only inspection of registered workspaces, their Docker Compose
state, and project-scoped logs and port information.

All tools follow the audit-first, human-in-the-loop philosophy:
- Observe and report only
- No deployment or container manipulation
- Constructor DI for all dependencies
- Workspace paths are resolved internally, never provided by the agent
"""

from __future__ import annotations

import structlog

from mcp_oranpi.application.parsers import (
    parse_compose_ps,
)
from mcp_oranpi.domain.contracts import CommandRunnerProtocol, WorkspaceResolverProtocol
from mcp_oranpi.domain.errors import (
    CONN_FAILED,
    CONN_TIMEOUT,
    DOCKER_NOT_FOUND,
    WS_DISABLED,
    WS_NO_COMPOSE,
    WS_NOT_FOUND,
    ToolError,
    ToolResult,
)
from mcp_oranpi.domain.truncation import truncate_text
from mcp_oranpi.domain.validation import (
    ValidationError,
    validate_line_limit,
    validate_workspace_id,
)

log = structlog.get_logger()


class WorkspaceTools:
    """Workspace inspection tools for OrangePi host.

    Provides read-only access to workspace metadata, Docker Compose state,
    container logs, and port information within registered workspaces.
    All operations are audit-only.

    Args:
        runner: CommandRunner instance for executing remote commands.
        resolver: WorkspaceResolver instance for path resolution.
    """

    def __init__(
        self,
        runner: CommandRunnerProtocol,
        resolver: WorkspaceResolverProtocol,
    ) -> None:
        self._runner = runner
        self._resolver = resolver

    # ── Workspace Listing ─────────────────────────────────────────────────────

    async def workspace_list(self) -> ToolResult:
        """List all registered workspaces.

        Returns:
            ToolResult with list of WorkspaceInfo.
        """
        log.info("workspace_list")

        workspaces = self._resolver.workspaces

        workspace_list_data = [
            {
                "id": ws.id,
                "path": ws.path,
                "status": ws.status.value,
                "has_compose": ws.has_compose,
                "has_git": ws.has_git,
                "project_type": ws.project_type,
                "detected_services": ws.detected_services,
                "docker_compose_project": ws.docker_compose_project,
            }
            for ws in workspaces.values()
        ]

        return ToolResult(
            data={
                "workspaces": workspace_list_data,
                "total_count": len(workspace_list_data),
            }
        )

    # ── Workspace Inspection ──────────────────────────────────────────────────

    async def workspace_inspect(self, workspace: str) -> ToolResult:
        """Inspect a workspace in detail.

        Args:
            workspace: Logical workspace identifier (e.g., "guardian").

        Returns:
            ToolResult with full WorkspaceInfo including detected services.
        """
        log.info("workspace_inspect", workspace_id=workspace)

        workspace_id = workspace

        try:
            validate_workspace_id(workspace_id)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        info = self._resolver.resolve(workspace_id)

        if info is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace not found: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        # Check workspace status
        if info.status.value == "disabled":
            return ToolResult(
                error=ToolError(
                    code=WS_DISABLED,
                    message=f"Workspace is disabled: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        if info.status.value == "unreachable":
            return ToolResult(
                error=ToolError(
                    code=WS_DISABLED,
                    message=f"Workspace is unreachable: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        # If active and has compose, run compose_services
        detected_services = list(info.detected_services)
        docker_compose_project = info.docker_compose_project

        if info.has_compose:
            path = self._resolver.resolve_path(workspace_id)
            if path:
                try:
                    result = await self._runner.run_in_workspace(
                        "compose_services",
                        workspace_path=path,
                    )
                    if result.exit_code == 0 and result.stdout.strip():
                        detected_services = [
                            s.strip() for s in result.stdout.strip().split("\n") if s.strip()
                        ]
                        docker_compose_project = workspace_id
                except TimeoutError:
                    pass  # Non-fatal - still return basic info
                except ConnectionError:
                    pass  # Non-fatal

        return ToolResult(
            data={
                "id": info.id,
                "path": info.path,
                "status": info.status.value,
                "has_compose": info.has_compose,
                "has_git": info.has_git,
                "project_type": info.project_type,
                "detected_services": detected_services,
                "docker_compose_project": docker_compose_project,
            }
        )

    # ── Workspace Docker PS ───────────────────────────────────────────────────

    async def workspace_docker_ps(self, workspace: str) -> ToolResult:
        """List Docker Compose containers in a workspace.

        Args:
            workspace: Logical workspace identifier.

        Returns:
            ToolResult with workspace containers from compose ps.
        """
        log.info("workspace_docker_ps", workspace_id=workspace)

        workspace_id = workspace

        try:
            validate_workspace_id(workspace_id)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        info = self._resolver.resolve(workspace_id)

        if info is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace not found: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        if info.status.value == "disabled":
            return ToolResult(
                error=ToolError(
                    code=WS_DISABLED,
                    message=f"Workspace is disabled: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        path = self._resolver.resolve_path(workspace_id)
        if path is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace path not resolved: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        # Check if workspace has compose
        if not info.has_compose:
            return ToolResult(
                error=ToolError(
                    code=WS_NO_COMPOSE,
                    message=f"Workspace has no Docker Compose: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run_in_workspace(
                "compose_ps",
                workspace_path=path,
            )
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while listing workspace containers",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        # compose_ps returns non-zero when no containers or no compose file
        if result.exit_code != 0:
            stderr_lower = result.stderr.lower()
            if "no such file" in stderr_lower or "no compose" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=WS_NO_COMPOSE,
                        message=f"docker compose not available in: {workspace_id}",
                        detail={"workspace_id": workspace_id},
                        retryable=False,
                    )
                )

        containers = parse_compose_ps(result.stdout)

        container_dicts = [
            {
                "name": c.name,
                "service": c.service,
                "state": c.state,
                "health": c.health,
                "ports": [
                    {
                        "host_port": p.host_port,
                        "container_port": p.container_port,
                        "protocol": p.protocol,
                        "host_ip": p.host_ip,
                    }
                    for p in c.ports
                ],
                "created": c.created,
            }
            for c in containers
        ]

        return ToolResult(
            data={
                "workspace": workspace_id,
                "path": path,
                "containers": container_dicts,
                "total_count": len(container_dicts),
            }
        )

    # ── Workspace Logs ────────────────────────────────────────────────────────

    async def workspace_logs(
        self,
        workspace: str,
        service: str | None = None,
        tail: int = 100,
        since: str | None = None,
    ) -> ToolResult:
        """Fetch logs from a Docker Compose service in a workspace.

        Args:
            workspace: Logical workspace identifier.
            service: Optional specific service name within the compose project.
            tail: Number of lines to fetch from the end.
            since: Optional ISO timestamp or relative time.

        Returns:
            ToolResult with log content and workspace context.
        """
        log.info(
            "workspace_logs",
            workspace_id=workspace,
            service=service,
            tail=tail,
        )

        workspace_id = workspace

        try:
            validate_workspace_id(workspace_id)
            validate_line_limit(tail)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        info = self._resolver.resolve(workspace_id)

        if info is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace not found: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        if info.status.value == "disabled":
            return ToolResult(
                error=ToolError(
                    code=WS_DISABLED,
                    message=f"Workspace is disabled: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        path = self._resolver.resolve_path(workspace_id)
        if path is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace path not resolved: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        try:
            if service is not None:
                result = await self._runner.run_in_workspace(
                    "compose_logs",
                    workspace_path=path,
                    service=service,
                    tail=tail,
                )
            else:
                result = await self._runner.run_in_workspace(
                    "compose_logs",
                    workspace_path=path,
                    tail=tail,
                )
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching workspace logs",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        # compose_logs may return non-zero if service not found or no logs
        if result.exit_code != 0:
            stderr_lower = result.stderr.lower()
            if "no such service" in stderr_lower or "no container" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=DOCKER_NOT_FOUND,
                        message=f"Service not found in workspace: {service}",
                        detail={"workspace_id": workspace_id, "service": service},
                        retryable=False,
                    )
                )

        # Apply truncation
        content = result.stdout
        truncated_text, meta = truncate_text(content)

        return ToolResult(
            data={
                "workspace": workspace_id,
                "service": service,
                "logs": truncated_text,
                "log_count": len(content.splitlines()) if content.strip() else 0,
                "truncated": meta.truncated,
            },
            truncated=meta.truncated,
        )

    # ── Workspace Ports ───────────────────────────────────────────────────────

    async def workspace_ports(self, workspace: str) -> ToolResult:
        """List exposed ports from Docker Compose services in a workspace.

        Args:
            workspace: Logical workspace identifier.

        Returns:
            ToolResult with port mappings from compose ps.
        """
        log.info("workspace_ports", workspace_id=workspace)

        workspace_id = workspace

        try:
            validate_workspace_id(workspace_id)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        info = self._resolver.resolve(workspace_id)

        if info is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace not found: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        if info.status.value == "disabled":
            return ToolResult(
                error=ToolError(
                    code=WS_DISABLED,
                    message=f"Workspace is disabled: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        path = self._resolver.resolve_path(workspace_id)
        if path is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace path not resolved: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run_in_workspace(
                "compose_ps",
                workspace_path=path,
            )
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while listing workspace ports",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        if result.exit_code != 0:
            stderr_lower = result.stderr.lower()
            if "no such file" in stderr_lower or "no compose" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=WS_NO_COMPOSE,
                        message=f"docker compose not available in: {workspace_id}",
                        detail={"workspace_id": workspace_id},
                        retryable=False,
                    )
                )

        containers = parse_compose_ps(result.stdout)

        # Extract port information from each container
        all_ports: list[dict[str, object]] = list()
        for container in containers:
            for port in container.ports:
                all_ports.append(
                    {
                        "service": container.service,
                        "container_name": container.name,
                        "host_port": port.host_port,
                        "container_port": port.container_port,
                        "protocol": port.protocol,
                        "host_ip": port.host_ip,
                    }
                )

        return ToolResult(
            data={
                "workspace": workspace_id,
                "path": path,
                "ports": all_ports,
                "total_count": len(all_ports),
            }
        )

    # ── Workspace Deploy Plan ─────────────────────────────────────────────────

    async def workspace_deploy(self, workspace: str) -> ToolResult:
        """Execute a production deployment for a workspace via the gatekeeper.

        This tool is MUTATIVE. It executes the 'deploy' command on the server
        which triggers the gatekeeper to git pull and docker compose up.
        It should only be invoked when explicitly authorized by the user.

        Args:
            workspace: Logical workspace identifier.

        Returns:
            ToolResult with manual deployment instructions.
        """
        log.info("workspace_deploy_plan", workspace_id=workspace)

        workspace_id = workspace

        try:
            validate_workspace_id(workspace_id)
        except ValidationError as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        info = self._resolver.resolve(workspace_id)

        if info is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace not found: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        if info.status.value == "disabled":
            return ToolResult(
                error=ToolError(
                    code=WS_DISABLED,
                    message=f"Workspace is disabled: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        path = self._resolver.resolve_path(workspace_id)
        if path is None:
            return ToolResult(
                error=ToolError(
                    code=WS_NOT_FOUND,
                    message=f"Workspace path not resolved: {workspace_id}",
                    detail={"workspace_id": workspace_id},
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run("workspace_deploy", project=workspace_id)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while executing deployment",
                    retryable=True,
                )
            )
        except ConnectionError:
            return ToolResult(
                error=ToolError(
                    code=CONN_FAILED,
                    message="SSH connection failed",
                    retryable=True,
                )
            )

        if result.exit_code != 0:
            truncated_out, _ = truncate_text(result.stdout)
            return ToolResult(
                error=ToolError(
                    code="DEPLOY_FAILED",
                    message=f"Deployment failed: {result.stderr[:200]}",
                    detail={"workspace_id": workspace_id, "stdout": truncated_out},
                    retryable=False,
                )
            )

        return ToolResult(
            data={
                "workspace": workspace_id,
                "path": path,
                "status": "deployed",
                "message": "Workspace deployment command executed successfully.",
                "output": result.stdout,
                "executed": True,
            }
        )
