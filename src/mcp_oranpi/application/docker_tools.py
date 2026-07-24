"""Docker audit tools for MCP OranPi.

Provides read-only inspection of Docker containers, images, and networks
on the remote OrangePi host.

All tools follow the audit-first, human-in-the-loop philosophy:
- Observe and report only
- No destructive operations
- Constructor DI for all dependencies
"""

from __future__ import annotations

import structlog

from mcp_oranpi.application.parsers import (
    parse_docker_inspect,
    parse_docker_logs,
    parse_docker_ps,
    parse_docker_stats,
    parse_ss_tulnp,
)
from mcp_oranpi.domain.contracts import CommandRunnerProtocol
from mcp_oranpi.domain.errors import (
    CONN_FAILED,
    CONN_TIMEOUT,
    DOCKER_NOT_FOUND,
    DOCKER_NOT_RUNNING,
    DOCKER_UNAVAILABLE,
    ToolError,
    ToolResult,
)
from mcp_oranpi.domain.models import PortOccupant
from mcp_oranpi.domain.truncation import truncate_list
from mcp_oranpi.domain.validation import validate_container_name, validate_line_limit

log = structlog.get_logger()


class DockerTools:
    """Docker inspection tools for OrangePi host.

    Provides read-only access to Docker container state, logs, and port
    mappings. All operations are audit-only — no container creation,
    starting, stopping, or removal.

    Args:
        runner: CommandRunner instance for executing remote commands.
    """

    def __init__(self, runner: CommandRunnerProtocol) -> None:
        self._runner = runner

    # ── Container Listing ─────────────────────────────────────────────────────

    async def docker_list_containers(
        self,
        all: bool = False,  # noqa: A002
        filter: str | None = None,  # noqa: A002
    ) -> ToolResult:
        """List Docker containers on the remote host.

        Args:
            all: Include stopped containers (docker ps -a).
            filter: Optional substring filter on container names.

        Returns:
            ToolResult with containers list and counts.
            Truncates at 500 containers.
        """
        log.info("docker_list_containers", all=all, filter=filter)

        command_key = "docker_ps_all" if all else "docker_ps"

        try:
            result = await self._runner.run(command_key)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while listing containers",
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

        # Exit code interpretation: application layer only
        if result.exit_code != 0:
            stderr_lower = result.stderr.lower()
            if "cannot connect to the docker daemon" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=DOCKER_UNAVAILABLE,
                        message="Docker daemon not responding",
                        retryable=False,
                    )
                )
            # Other errors — docker ps returns non-zero when daemon unavailable
            return ToolResult(
                error=ToolError(
                    code=DOCKER_UNAVAILABLE,
                    message=f"docker ps failed: {result.stderr[:200]}",
                    retryable=False,
                )
            )

        containers = parse_docker_ps(result.stdout)

        # Post-filter by name if requested
        if filter:
            containers = [c for c in containers if filter.lower() in c.name.lower()]

        # Truncate to max 500
        containers, meta = truncate_list(containers, max_length=500)

        container_dicts = [
            {
                "id": c.id,
                "name": c.name,
                "image": c.image,
                "status": c.status,
                "created": c.created,
                "ports": [
                    {
                        "host_port": p.host_port,
                        "container_port": p.container_port,
                        "protocol": p.protocol,
                        "host_ip": p.host_ip,
                    }
                    for p in c.ports
                ],
            }
            for c in containers
        ]

        return ToolResult(
            data={
                "containers": container_dicts,
                "total_count": meta.total_count or len(container_dicts),
                "returned_count": meta.returned_count or len(container_dicts),
                "truncated": meta.truncated,
            },
            truncated=meta.truncated,
        )

    # ── Container Inspection ──────────────────────────────────────────────────

    async def docker_inspect_container(self, container: str) -> ToolResult:
        """Inspect a Docker container in detail.

        Args:
            container: Container name or ID.

        Returns:
            ToolResult with full ContainerDetail.
        """
        log.info("docker_inspect_container", container=container)

        try:
            validate_container_name(container)
        except Exception as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run("docker_inspect", container=container)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while inspecting container",
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

        # Exit code 1: container not found
        if result.exit_code == 1:
            stderr_lower = result.stderr.lower()
            if "no such object" in stderr_lower or "no such container" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=DOCKER_NOT_FOUND,
                        message=f"Container not found: {container}",
                        detail={"container": container},
                        retryable=False,
                    )
                )

        if result.exit_code != 0:
            return ToolResult(
                error=ToolError(
                    code=DOCKER_NOT_FOUND,
                    message=f"docker inspect failed: {result.stderr[:200]}",
                    detail={"container": container},
                    retryable=False,
                )
            )

        detail = parse_docker_inspect(result.stdout)

        if detail is None:
            return ToolResult(
                error=ToolError(
                    code=DOCKER_NOT_FOUND,
                    message=f"Could not parse container inspection: {container}",
                    detail={"container": container},
                    retryable=False,
                )
            )

        return ToolResult(
            data={
                "id": detail.id,
                "name": detail.name,
                "image": detail.image,
                "status": detail.status,
                "created": detail.created,
                "ports": [
                    {
                        "host_port": p.host_port,
                        "container_port": p.container_port,
                        "protocol": p.protocol,
                        "host_ip": p.host_ip,
                    }
                    for p in detail.ports
                ],
                "networks": detail.networks,
                "labels": detail.labels,
                "env": detail.env,  # Already names-only from parser
                "mounts": [
                    {
                        "source": m.source,
                        "destination": m.destination,
                        "mode": m.mode,
                        "type": m.type,
                    }
                    for m in detail.mounts
                ],
                "health": (
                    {
                        "status": detail.health.status,
                        "failing_streak": detail.health.failing_streak,
                        "last_output": detail.health.last_output,
                    }
                    if detail.health
                    else None
                ),
                "restart_policy": detail.restart_policy,
            }
        )

    # ── Container Logs ────────────────────────────────────────────────────────

    async def docker_container_logs(
        self,
        container: str,
        tail: int = 100,
        since: str | None = None,
        until: str | None = None,
    ) -> ToolResult:
        """Fetch logs from a Docker container.

        Args:
            container: Container name or ID.
            tail: Number of lines to fetch from the end.
            since: ISO timestamp or relative time (e.g., "1h").
            until: ISO timestamp or relative time.

        Returns:
            ToolResult with DockerLogResult.
        """
        log.info("docker_container_logs", container=container, tail=tail)

        try:
            validate_container_name(container)
            validate_line_limit(tail)
        except Exception as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        # Build params dict, excluding None values
        try:
            if since is not None and until is not None:
                result = await self._runner.run(
                    "docker_logs", container=container, tail=tail, since=since, until=until
                )
            elif since is not None:
                result = await self._runner.run(
                    "docker_logs", container=container, tail=tail, since=since
                )
            elif until is not None:
                result = await self._runner.run(
                    "docker_logs", container=container, tail=tail, until=until
                )
            else:
                result = await self._runner.run("docker_logs", container=container, tail=tail)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching container logs",
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

        # Exit code non-zero may mean container not running or logs unavailable
        if result.exit_code != 0:
            stderr_lower = result.stderr.lower()
            if "no such container" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=DOCKER_NOT_FOUND,
                        message=f"Container not found: {container}",
                        detail={"container": container},
                        retryable=False,
                    )
                )
            # Container might be stopped — still return logs if we got any
            if not result.stdout and result.stderr:
                return ToolResult(
                    error=ToolError(
                        code=DOCKER_NOT_RUNNING,
                        message=f"Container not running or logs unavailable: {container}",
                        detail={"container": container},
                        retryable=False,
                    )
                )

        log_result = parse_docker_logs(result.stdout, container)

        return ToolResult(
            data={
                "container": log_result.container,
                "log_count": log_result.log_count,
                "logs": log_result.logs,
                "truncated": log_result.truncated,
            },
            truncated=log_result.truncated,
        )

    # ── Container Stats ───────────────────────────────────────────────────────

    async def docker_container_stats(self, container: str) -> ToolResult:
        """Get resource usage statistics for a container.

        Args:
            container: Container name or ID.

        Returns:
            ToolResult with ContainerStats.
        """
        log.info("docker_container_stats", container=container)

        try:
            validate_container_name(container)
        except Exception as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run("docker_stats", container=container)
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching container stats",
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

        # Exit code 1: container found but not running
        if result.exit_code == 1:
            stderr_lower = result.stderr.lower()
            if "no such container" in stderr_lower:
                return ToolResult(
                    error=ToolError(
                        code=DOCKER_NOT_FOUND,
                        message=f"Container not found: {container}",
                        detail={"container": container},
                        retryable=False,
                    )
                )
            return ToolResult(
                error=ToolError(
                    code=DOCKER_NOT_RUNNING,
                    message=f"Container not running: {container}",
                    detail={"container": container},
                    retryable=False,
                )
            )

        if result.exit_code != 0:
            return ToolResult(
                error=ToolError(
                    code=DOCKER_NOT_RUNNING,
                    message=f"docker stats failed: {result.stderr[:200]}",
                    detail={"container": container},
                    retryable=False,
                )
            )

        stats = parse_docker_stats(result.stdout)

        if stats is None:
            return ToolResult(
                error=ToolError(
                    code=DOCKER_NOT_RUNNING,
                    message=f"Could not parse stats for: {container}",
                    detail={"container": container},
                    retryable=False,
                )
            )

        return ToolResult(
            data={
                "container": stats.container,
                "cpu_percent": stats.cpu_percent,
                "memory_usage_mb": stats.memory_usage_mb,
                "memory_limit_mb": stats.memory_limit_mb,
                "memory_percent": stats.memory_percent,
                "network_io": (
                    {
                        "bytes_in": stats.network_io.bytes_in,
                        "bytes_out": stats.network_io.bytes_out,
                    }
                    if stats.network_io
                    else None
                ),
                "block_io": (
                    {
                        "bytes_read": stats.block_io.bytes_read,
                        "bytes_written": stats.block_io.bytes_written,
                    }
                    if stats.block_io
                    else None
                ),
                "pids": stats.pids,
            }
        )

    # ── Port Inspection ───────────────────────────────────────────────────────

    async def docker_inspect_ports(
        self,
        container: str | None = None,
        protocol: str = "tcp",
    ) -> ToolResult:
        """Inspect Docker port bindings and host-level port occupancy.

        Args:
            container: Optional specific container to inspect.
            protocol: Filter by protocol (tcp/udp). Default tcp.

        Returns:
            ToolResult with list of PortOccupant.
        """
        log.info("docker_inspect_ports", container=container, protocol=protocol)

        if container:
            try:
                validate_container_name(container)
            except Exception as e:
                return ToolResult(
                    error=ToolError(
                        code="VALID_PARAM_INVALID",
                        message=str(e),
                        retryable=False,
                    )
                )

        # Get host-level port occupancy from ss
        try:
            ss_result = await self._runner.run("ss_tulnp")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out during port scan",
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

        occupied_ports = parse_ss_tulnp(ss_result.stdout)

        # Filter by protocol
        occupied_ports = [p for p in occupied_ports if protocol in p.protocol]

        # If container specified, get its port bindings
        docker_port_bindings: list[PortOccupant] = list()

        if container:
            try:
                inspect_result = await self._runner.run("docker_inspect", container=container)
            except TimeoutError:
                return ToolResult(
                    error=ToolError(
                        code=CONN_TIMEOUT,
                        message="Command timed out while inspecting container ports",
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

            if inspect_result.exit_code == 0:
                detail = parse_docker_inspect(inspect_result.stdout)
                if detail:
                    docker_port_bindings = [
                        PortOccupant(
                            host_port=p.host_port,
                            container_port=p.container_port,
                            protocol=p.protocol,
                            container_name=detail.name,
                            container_id=detail.id[:12],
                            host_ip=p.host_ip,
                        )
                        for p in detail.ports
                        if p.host_port > 0
                    ]

        # Cross-reference: build final list
        all_ports: list[dict[str, object]] = list()

        # Add Docker container ports if specific container requested
        for binding in docker_port_bindings:
            all_ports.append(
                {
                    "host_port": binding.host_port,
                    "container_port": binding.container_port,
                    "protocol": binding.protocol,
                    "container_name": binding.container_name,
                    "container_id": binding.container_id,
                    "host_ip": binding.host_ip,
                    "process": binding.process,
                }
            )

        # Add ss-detected ports that are docker proxies or any unclaimed ports
        for occ in occupied_ports:
            if occ.process and "docker-proxy" in occ.process.lower():
                all_ports.append(
                    {
                        "port": occ.port,
                        "protocol": occ.protocol,
                        "process": occ.process,
                        "container": occ.container,
                    }
                )
            elif container is None:
                # When no specific container, show all occupied ports
                all_ports.append(
                    {
                        "port": occ.port,
                        "protocol": occ.protocol,
                        "process": occ.process,
                        "container": occ.container,
                    }
                )

        return ToolResult(
            data={
                "container": container,
                "protocol": protocol,
                "occupied_ports": all_ports,
            }
        )
