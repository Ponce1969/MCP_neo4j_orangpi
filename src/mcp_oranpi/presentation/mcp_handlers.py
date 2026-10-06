"""MCP protocol handler and tool definitions for MCP OranPi.

This module is the ONLY module that knows about MCP types (mcp.types).
It receives application-layer tool instances and dispatches MCP calls to them.

Tool definitions (22 total):
- Docker (5): docker_list_containers, docker_inspect_container,
              docker_container_logs, docker_container_stats, docker_inspect_ports
- Network (4): network_scan_ports, network_suggest_port,
               network_inspect_bindings, network_tailscale_status
- System (5): system_cpu_usage, system_memory_usage, system_disk_usage,
              system_temperatures, system_service_status
- Logs (3): logs_docker, logs_systemd, logs_file
- Workspace (5): workspace_list, workspace_inspect, workspace_docker_ps,
                 workspace_logs, workspace_ports
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine

import mcp.types as types

from mcp_oranpi.application.docker_tools import DockerTools
from mcp_oranpi.application.logs_tools import LogsTools
from mcp_oranpi.application.network_tools import NetworkTools
from mcp_oranpi.application.system_tools import SystemTools
from mcp_oranpi.application.workspace_tools import WorkspaceTools
from mcp_oranpi.domain.errors import ToolResult
from mcp_oranpi.domain.redaction import redact_json_response

# ── Tool Definitions ───────────────────────────────────────────────────────────


TOOL_DEFINITIONS: list[types.Tool] = [
    # ── Docker Tools (5) ────────────────────────────────────────────────────
    types.Tool(
        name="docker_list_containers",
        description="List Docker containers on the remote host. Returns container summaries "
        "with ID, name, image, status, and port mappings. "
        "Errors: CONN_FAILED (SSH unreachable), CONN_TIMEOUT (command timed out), "
        "DOCKER_UNAVAILABLE (daemon not responding).",
        inputSchema={
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include stopped containers (docker ps -a)",
                },
                "filter": {
                    "type": "string",
                    "description": "Optional substring filter on container names",
                },
            },
        },
    ),
    types.Tool(
        name="docker_inspect_container",
        description="Inspect a Docker container in full detail. Returns all metadata including "
        "networks, labels, environment variables, mounts, health status, and restart policy. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, DOCKER_NOT_FOUND (container does not exist), "
        "VALID_PARAM_INVALID (invalid container name).",
        inputSchema={
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": "Container name or ID (exact match)",
                },
            },
            "required": ["container"],
        },
    ),
    types.Tool(
        name="docker_container_logs",
        description="Fetch logs from a Docker container with optional time filtering. "
        "Returns log lines with truncation if output exceeds 50KB. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, DOCKER_NOT_FOUND (container not found), "
        "DOCKER_NOT_RUNNING (container not running or logs unavailable), "
        "VALID_PARAM_INVALID (invalid parameters).",
        inputSchema={
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": "Container name or ID",
                },
                "tail": {
                    "type": "integer",
                    "default": 100,
                    "description": "Number of lines to fetch from the end",
                },
                "since": {
                    "type": "string",
                    "description": "ISO timestamp or relative time (e.g., '1h') to fetch from",
                },
                "until": {
                    "type": "string",
                    "description": "ISO timestamp or relative time to fetch until",
                },
            },
            "required": ["container"],
        },
    ),
    types.Tool(
        name="docker_container_stats",
        description="Get resource usage statistics for a Docker container. Returns CPU %, "
        "memory usage/limit/%, network IO, block IO, and PID count. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, DOCKER_NOT_FOUND (container not found), "
        "DOCKER_NOT_RUNNING (container not running).",
        inputSchema={
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": "Container name or ID",
                },
            },
            "required": ["container"],
        },
    ),
    types.Tool(
        name="docker_inspect_ports",
        description="Inspect Docker port bindings and host-level port occupancy. "
        "When container is specified, returns its port mappings. "
        "When container is omitted, returns all ports occupied by docker-proxy processes. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, VALID_PARAM_INVALID (invalid container name).",
        inputSchema={
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": "Optional specific container to inspect",
                },
                "protocol": {
                    "type": "string",
                    "default": "tcp",
                    "description": "Protocol filter: 'tcp' or 'udp'",
                },
            },
        },
    ),
    # ── Network Tools (4) ───────────────────────────────────────────────────
    types.Tool(
        name="network_scan_ports",
        description="Scan network ports within a range on the remote host using 'ss -tulnp'. "
        "Returns occupied ports with process/container information. "
        "Maximum scan range is 10000 ports. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, NET_SCAN_RANGE_INVALID (range out of bounds), "
        "NET_SCAN_TIMEOUT (scan took too long).",
        inputSchema={
            "type": "object",
            "properties": {
                "range_start": {
                    "type": "integer",
                    "default": 1,
                    "description": "First port to check (1-65535)",
                },
                "range_end": {
                    "type": "integer",
                    "default": 1024,
                    "description": "Last port to check (1-65535)",
                },
                "protocol": {
                    "type": "string",
                    "default": "tcp",
                    "description": "Protocol filter: 'tcp' or 'udp'",
                },
            },
        },
    ),
    types.Tool(
        name="network_suggest_port",
        description="Suggest available ports near a preferred port. Searches within a range "
        "above and below the preferred port, preferring close ports. "
        "Returns the preferred port status and up to N suggestions. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, VALID_PARAM_INVALID (port out of range).",
        inputSchema={
            "type": "object",
            "properties": {
                "preferred_port": {
                    "type": "integer",
                    "description": "The port the caller would prefer to use",
                },
                "range": {
                    "type": "integer",
                    "default": 100,
                    "description": "Search within this many ports above and below preferred",
                },
                "count": {
                    "type": "integer",
                    "default": 5,
                    "description": "Return up to this many suggestions",
                },
            },
            "required": ["preferred_port"],
        },
    ),
    types.Tool(
        name="network_inspect_bindings",
        description="Inspect all active network bindings on the remote host. "
        "Returns each listening socket with its exact local_address (the real "
        "OS bind, e.g. 100.106.85.109:8003 or [::]:7474, never an assumed "
        "wildcard), port, protocol, process, pid, and container information. "
        "Errors: CONN_FAILED, CONN_TIMEOUT.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="network_tailscale_status",
        description="Get Tailscale VPN status on the remote host. Returns online status, "
        "Tailscale IP, public IP, hostname, DNS name, and peer list. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, NET_TAILSCALE_NOT_INSTALLED (not installed).",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    # ── System Tools (5) ────────────────────────────────────────────────────
    types.Tool(
        name="system_cpu_usage",
        description="Get CPU usage snapshot from the remote host. Returns global CPU %, "
        "per-core percentages, load averages (1m, 5m, 15m), and uptime in seconds. "
        "Note: The duration parameter is accepted but ignored in v0.1 "
        "(always returns a single snapshot, not historical). "
        "Errors: CONN_FAILED, CONN_TIMEOUT.",
        inputSchema={
            "type": "object",
            "properties": {
                "duration": {
                    "type": "string",
                    "default": "5m",
                    "enum": ["5m", "15m", "30m", "1h", "6h", "24h"],
                    "description": "Duration for the snapshot (ignored in v0.1)",
                },
            },
        },
    ),
    types.Tool(
        name="system_memory_usage",
        description="Get memory usage snapshot from the remote host. Returns total, used, "
        "available RAM in MB, usage percentage, and swap statistics. "
        "Errors: CONN_FAILED, CONN_TIMEOUT.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="system_disk_usage",
        description="Get disk usage for mounted filesystems. Returns device, mount point, "
        "total/used/available GB, usage %, and filesystem type. "
        "Optionally filter by specific mount point path. "
        "Errors: CONN_FAILED, CONN_TIMEOUT.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional mount point path to filter results",
                },
            },
        },
    ),
    types.Tool(
        name="system_temperatures",
        description="Get hardware temperature sensor readings. "
        "Uses Linux thermal zones (/sys/class/thermal/) on all ARM boards, "
        "falls back to vcgencmd on Raspberry Pi. "
        "Returns sensor name, temperature in Celsius, and source. "
        "On OrangePi 5 Plus, reports SoC, bigcore, littlecore, GPU, and NPU temperatures. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, SYS_SENSORS_UNAVAILABLE.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="system_service_status",
        description="Get systemd service status. When service is omitted, lists all services. "
        "When service is specified, returns detailed status for that service. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, SYS_SERVICE_NOT_FOUND (service not found), "
        "VALID_PARAM_INVALID (invalid service name).",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Optional specific service name to query",
                },
            },
        },
    ),
    # ── Logs Tools (3) ──────────────────────────────────────────────────────
    types.Tool(
        name="logs_docker",
        description="Fetch logs from a Docker container (alias for docker_container_logs). "
        "Returns log lines with line count and truncation flag. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, DOCKER_NOT_FOUND (container not found), "
        "VALID_PARAM_INVALID (invalid parameters).",
        inputSchema={
            "type": "object",
            "properties": {
                "container": {
                    "type": "string",
                    "description": "Container name or ID",
                },
                "tail": {
                    "type": "integer",
                    "default": 100,
                    "description": "Number of lines to fetch from the end",
                },
            },
            "required": ["container"],
        },
    ),
    types.Tool(
        name="logs_systemd",
        description="Fetch logs from the systemd journal. Supports filtering by unit, "
        "priority level (emerg/debug), and time range. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, LOG_UNIT_NOT_FOUND (unit unavailable), "
        "LOG_QUERY_FAILED (journalctl rejected the query), "
        "VALID_PARAM_INVALID (invalid parameters).",
        inputSchema={
            "type": "object",
            "properties": {
                "unit": {
                    "type": "string",
                    "description": "Optional systemd unit name to filter by",
                },
                "priority": {
                    "type": "string",
                    "default": "info",
                    "description": "Priority level: emerg, alert, crit, err, warning, notice, info, debug",
                },
                "tail": {
                    "type": "integer",
                    "default": 100,
                    "description": "Number of lines to fetch from the end",
                },
                "since": {
                    "type": "string",
                    "description": "ISO timestamp or relative time (e.g., '1 hour ago')",
                },
                "until": {
                    "type": "string",
                    "description": "ISO timestamp or relative time upper bound (e.g., '1 hour ago')",
                },
            },
        },
    ),
    types.Tool(
        name="logs_file",
        description="Read a log file from the remote host with security validation. "
        "Validates path against allowed directories (/var/log, /home, /opt), "
        "checks file size (max 50MB), resolves symlinks, and applies output truncation. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, LOG_PATH_FORBIDDEN (path outside allowed dirs), "
        "LOG_FILE_NOT_FOUND (file does not exist), LOG_FILE_OVERSIZED (exceeds size limit), "
        "LOG_FILE_NOT_READABLE (binary file detected).",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the log file on remote host",
                },
                "tail": {
                    "type": "integer",
                    "default": 1000,
                    "description": "Number of lines to read from the end of the file",
                },
                "since": {
                    "type": "string",
                    "description": "Optional ISO timestamp (not supported in v0.1)",
                },
            },
            "required": ["path"],
        },
    ),
    # ── Workspace Tools (5) ─────────────────────────────────────────────────
    types.Tool(
        name="workspace_list",
        description="List all registered workspaces. Returns workspace metadata including "
        "ID, path, status (active/disabled/unreachable), and indicators "
        "(has_compose, has_git, project_type, detected_services). "
        "Errors: none (returns empty list if no workspaces registered).",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="workspace_inspect",
        description="Inspect a workspace in full detail. Returns workspace metadata, "
        "detected services from docker compose config, and project type. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, WS_NOT_FOUND (workspace not registered), "
        "WS_DISABLED (workspace disabled or unreachable), "
        "VALID_PARAM_INVALID (invalid workspace ID).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Logical workspace identifier (e.g., 'guardian')",
                },
            },
            "required": ["workspace"],
        },
    ),
    types.Tool(
        name="workspace_docker_ps",
        description="List Docker Compose containers in a workspace. Returns container details "
        "including name, service, state, health, ports, and created time. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, WS_NOT_FOUND (workspace not registered), "
        "WS_DISABLED (workspace disabled or unreachable), "
        "WS_NO_COMPOSE (no docker-compose.yml found), "
        "VALID_PARAM_INVALID (invalid workspace ID).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Logical workspace identifier (e.g., 'guardian')",
                },
            },
            "required": ["workspace"],
        },
    ),
    types.Tool(
        name="workspace_logs",
        description="Fetch logs from a Docker Compose service in a workspace. "
        "Optionally filter by specific service name. "
        "Returns log content with line count and truncation flag. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, WS_NOT_FOUND, "
        "WS_DISABLED, DOCKER_NOT_FOUND (service not found in workspace), "
        "VALID_PARAM_INVALID (invalid parameters).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Logical workspace identifier (e.g., 'guardian')",
                },
                "service": {
                    "type": "string",
                    "description": "Optional specific service name within the compose project",
                },
                "tail": {
                    "type": "integer",
                    "default": 100,
                    "description": "Number of lines to fetch from the end",
                },
            },
            "required": ["workspace"],
        },
    ),
    types.Tool(
        name="workspace_ports",
        description="List exposed ports from Docker Compose services in a workspace. "
        "Returns port mappings with service, container, host/container ports, protocol, and IP. "
        "Errors: CONN_FAILED, CONN_TIMEOUT, WS_NOT_FOUND, "
        "WS_DISABLED, WS_NO_COMPOSE (no docker-compose.yml found), "
        "VALID_PARAM_INVALID (invalid workspace ID).",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Logical workspace identifier (e.g., 'guardian')",
                },
            },
            "required": ["workspace"],
        },
    ),
    types.Tool(
        name="workspace_deploy",
        description="Execute a production deployment for a workspace via the secure gatekeeper. "
        "WARNING: This executes a mutative 'deploy' command on the OrangePi server. "
        "It pulls the latest code from git and runs 'docker compose up --build -d'. "
        "Use this only when explicitly instructed by the user to deploy the workspace. "
        "Errors: WS_NOT_FOUND, WS_DISABLED, VALID_PARAM_INVALID.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Logical workspace identifier (e.g., 'pedidos_multi')",
                },
            },
            "required": ["workspace"],
        },
    ),
]


# ── MCP Handler ───────────────────────────────────────────────────────────────


class MCPHandler:
    """MCP protocol handler for OranPi audit tools.

    Dispatches MCP tool calls to application-layer tool classes.
    This is the ONLY module that knows about MCP types.

    The handler receives tool instances via constructor injection (DI).
    It maintains a dispatch table mapping tool names to async methods.
    """

    def __init__(
        self,
        docker_tools: DockerTools,
        network_tools: NetworkTools,
        system_tools: SystemTools,
        logs_tools: LogsTools,
        workspace_tools: WorkspaceTools,
    ) -> None:
        self._docker = docker_tools
        self._network = network_tools
        self._system = system_tools
        self._logs = logs_tools
        self._workspace = workspace_tools

        # Dispatch table: tool name -> bound async method
        self._dispatch: dict[str, Callable[..., Coroutine[None, None, ToolResult]]] = {
            # Docker tools
            "docker_list_containers": self._docker.docker_list_containers,
            "docker_inspect_container": self._docker.docker_inspect_container,
            "docker_container_logs": self._docker.docker_container_logs,
            "docker_container_stats": self._docker.docker_container_stats,
            "docker_inspect_ports": self._docker.docker_inspect_ports,
            # Network tools
            "network_scan_ports": self._network.network_scan_ports,
            "network_suggest_port": self._network.network_suggest_port,
            "network_inspect_bindings": self._network.network_inspect_bindings,
            "network_tailscale_status": self._network.network_tailscale_status,
            # System tools
            "system_cpu_usage": self._system.system_cpu_usage,
            "system_memory_usage": self._system.system_memory_usage,
            "system_disk_usage": self._system.system_disk_usage,
            "system_temperatures": self._system.system_temperatures,
            "system_service_status": self._system.system_service_status,
            # Logs tools
            "logs_docker": self._logs.logs_docker,
            "logs_systemd": self._logs.logs_systemd,
            "logs_file": self._logs.logs_file,
            # Workspace tools
            "workspace_list": self._workspace.workspace_list,
            "workspace_inspect": self._workspace.workspace_inspect,
            "workspace_docker_ps": self._workspace.workspace_docker_ps,
            "workspace_logs": self._workspace.workspace_logs,
            "workspace_ports": self._workspace.workspace_ports,
            "workspace_deploy": self._workspace.workspace_deploy,
        }

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> list[types.TextContent]:
        """Dispatch a tool call and return MCP content blocks.

        Args:
            name: The tool name from the MCP request.
            arguments: The arguments dict from the MCP request.

        Returns:
            A list of TextContent blocks (normally just one).

        Raises:
            ValueError: If the tool name is not recognized.
        """
        handler = self._dispatch.get(name)
        if handler is None:
            raise ValueError(f"Unknown tool: {name!r}")

        # The 'all' parameter for docker_list_containers is a Python keyword.
        # Unpacking a dict with 'all' key works fine: func(**{'all': True})
        result = await handler(**arguments)

        return self._to_mcp_content(result)

    @staticmethod
    def _to_mcp_content(result: ToolResult) -> list[types.TextContent]:
        """Convert a ToolResult to MCP TextContent blocks.

        Applies secret redaction to ALL output before sending to the agent.
        Success: returns data as JSON.
        Error: returns error code and message as JSON.

        Args:
            result: The ToolResult from an application-layer tool.

        Returns:
            A single-element list of TextContent with the JSON payload.
        """
        if result.error is not None:
            error_data: dict[str, object] = {
                "error": {
                    "code": result.error.code,
                    "message": result.error.message,
                }
            }
            if result.error.detail is not None:
                error_data["error"]["detail"] = result.error.detail  # type: ignore[index]
            if result.truncated:
                error_data["truncated"] = True
            
            # Apply redaction to prevent secret leaks in error stdout/stderr
            redacted_error = redact_json_response(error_data)
            return [types.TextContent(type="text", text=json.dumps(redacted_error, default=str))]

        # Success — wrap data in "data" key per MCP spec
        # Apply secret redaction to prevent credential leaks
        redacted_data = redact_json_response(result.data)
        output: dict[str, object] = {"data": redacted_data}
        if result.truncated:
            output["truncated"] = True
        return [types.TextContent(type="text", text=json.dumps(output, default=str))]
