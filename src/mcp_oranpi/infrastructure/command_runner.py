"""Safe command execution for MCP OranPi.

Implements the ALLOWED_COMMANDS whitelist from spec 006.
CommandRunner sits between SSHClient (infrastructure) and the
Application layer. It constructs commands from templates and
delegates execution to SSHClient.

Design decisions (Phase 2 corrections):
- CommandRunner handles cwd wrapping (cd {workspace_path} && {command}).
- SSHClient has NO knowledge of workspaces, paths, or cwd.
- Exit code interpretation belongs to the Application layer.
- CommandRunner does NOT interpret CommandResult semantics.
- All parameters are validated BEFORE command construction.
- No command accepts raw user input as arguments.
"""

from __future__ import annotations

import re
import shlex

import structlog

from mcp_oranpi.domain.contracts import CommandResultProtocol, SSHClientProtocol
from mcp_oranpi.domain.validation import (
    validate_container_name,
    validate_duration,
    validate_line_limit,
    validate_port,
    validate_priority,
    validate_service_name,
    validate_workspace_id,
)

log = structlog.get_logger()


# ── Allowed Commands ─────────────────────────────────────────────────────────
# Whitelist of commands that can be executed on the remote host.
# Commands are templates — parameters are interpolated safely, never raw input.

ALLOWED_COMMANDS: dict[str, str] = {
    # Docker audit
    "docker_ps": "docker ps --format json",
    "docker_ps_all": "docker ps -a --format json",
    "docker_inspect": "docker inspect --format json",
    "docker_logs": "docker logs",
    "docker_stats": "docker stats --no-stream --format json",
    "docker_port": "docker port",
    # Network audit
    "ss_tulnp": "ss -tulnp",
    "tailscale_status": "tailscale status --json",
    # System audit
    "top_bn1": "top -bn1",
    "free_m": "free -m",
    "df_h": "df -h",
    "df_t": "df -T",
    "vcgencmd_measure_temp": "vcgencmd measure_temp",
    "thermal_zones": (
        "for z in /sys/class/thermal/thermal_zone*; "
        'do echo "$(cat $z/type 2>/dev/null||echo unknown)'
        '|$(cat $z/temp 2>/dev/null||echo 0)"; done'
    ),
    "systemctl_status": "systemctl status",
    "systemctl_list": "systemctl list-units --type=service",
    "hostname": "hostname",
    "uptime": "uptime",
    # Logs
    "journalctl": "journalctl",
    # File inspection
    "stat_size": "stat -c %s",
    "readlink": "readlink -f",
    "tail_file": "tail -n",
    # Workspace-scoped Docker Compose (executed from workspace directory)
    "compose_ps": "docker compose ps --format json",
    "compose_config": "docker compose config",
    "compose_logs": "docker compose logs",
    "compose_services": "docker compose config --services",
    "workspace_deploy": "deploy",
}


# ── Parameter Templates ──────────────────────────────────────────────────────
# These are the parameters accepted by each command key.
# Used by build_command to construct the final command string.

_COMMAND_PARAMS: dict[str, list[str]] = {
    "docker_ps": [],
    "docker_ps_all": [],
    "docker_inspect": ["container"],
    "docker_logs": ["container", "tail", "since", "until"],
    "docker_stats": ["container"],
    "docker_port": ["container"],
    "ss_tulnp": [],
    "tailscale_status": [],
    "top_bn1": [],
    "free_m": [],
    "df_h": [],
    "df_t": [],
    "vcgencmd_measure_temp": [],
    "thermal_zones": [],
    "systemctl_status": ["service"],
    "systemctl_list": [],
    "hostname": [],
    "uptime": [],
    "journalctl": ["unit", "lines", "priority", "since", "until"],
    "stat_size": ["path"],
    "readlink": ["path"],
    "tail_file": ["path", "lines"],
    "compose_ps": [],
    "compose_config": [],
    "compose_logs": ["service", "tail"],
    "compose_services": [],
    "workspace_deploy": ["project"],
}


class CommandRunner:
    """Safe command execution over SSH.

    Builds commands from ALLOWED_COMMANDS templates with validated
    parameters. Delegates execution to SSHClient.

    Usage::

        runner = CommandRunner(ssh_client)
        result = await runner.run("docker_ps")
        result = await runner.run("docker_logs", container="nginx", tail=50)
        result = await runner.run_in_workspace("compose_ps", "/home/cerra/guardian")
    """

    def __init__(self, ssh_client: SSHClientProtocol) -> None:
        self._ssh = ssh_client

    def build_command(
        self,
        command_key: str,
        **params: str | int,
    ) -> str:
        """Build a command string from an ALLOWED_COMMANDS template.

        Validates all parameters before constructing the command.
        No raw user input is ever passed to the remote host.

        Args:
            command_key: Key from ALLOWED_COMMANDS (e.g., "docker_ps").
            **params: Validated parameters for the command template.

        Returns:
            The constructed command string.

        Raises:
            ValueError: If command_key is not in ALLOWED_COMMANDS.
            ValueError: If required parameters are missing.
        """
        if command_key not in ALLOWED_COMMANDS:
            raise ValueError(
                f"Unknown command key: {command_key!r}. "
                f"Allowed keys: {', '.join(sorted(ALLOWED_COMMANDS))}"
            )

        template = ALLOWED_COMMANDS[command_key]
        expected_params = _COMMAND_PARAMS.get(command_key, [])

        # Validate all parameters
        self._validate_params(command_key, expected_params, params)

        # Build the command from template + parameters
        return self._construct_command(template, command_key, params)

    async def run(
        self,
        command_key: str,
        *,
        timeout: int | None = None,
        **params: str | int,
    ) -> CommandResultProtocol:
        """Execute an allowed command on the remote host.

        Args:
            command_key: Key from ALLOWED_COMMANDS.
            timeout: Command timeout in seconds. Uses SSH config default if None.
            **params: Validated parameters for the command.

        Returns:
            CommandResult with raw exit_code, stdout, stderr.

        Raises:
            ValueError: If command_key is not allowed or parameters fail validation.
            asyncio.TimeoutError: If the command exceeds the timeout.
            ConnectionError: If SSH is not connected.
        """
        command = self.build_command(command_key, **params)

        log.info(
            "command_run",
            command_key=command_key,
            command=command[:100],
        )

        return await self._ssh.execute(command, timeout=timeout)

    async def run_in_workspace(
        self,
        command_key: str,
        workspace_path: str,
        *,
        timeout: int | None = None,
        **params: str | int,
    ) -> CommandResultProtocol:
        """Execute an allowed command from a workspace directory.

        Wraps the command with 'cd {workspace_path} && {command}'.
        The workspace_path is resolved internally by the
        WorkspaceResolver — it is NEVER provided by the agent.

        Correction 1: cwd handling belongs to CommandRunner,
        NOT to SSHClient.

        Args:
            command_key: Key from ALLOWED_COMMANDS.
            workspace_path: Absolute remote path resolved from workspace ID.
            timeout: Command timeout in seconds.
            **params: Validated parameters.

        Returns:
            CommandResult with raw exit_code, stdout, stderr.

        Raises:
            ValueError: If command_key or parameters are invalid.
            asyncio.TimeoutError: If the command exceeds the timeout.
            ConnectionError: If SSH is not connected.
        """
        command = self.build_command(command_key, **params)
        wrapped_command = f"cd {shlex.quote(workspace_path)} && {command}"

        log.info(
            "command_run_in_workspace",
            command_key=command_key,
            workspace_path=workspace_path,
            command=command[:100],
        )

        return await self._ssh.execute(wrapped_command, timeout=timeout)

    # ── Parameter Validation ───────────────────────────────────────────────

    def _validate_params(
        self,
        command_key: str,
        expected_params: list[str],
        params: dict[str, str | int],
    ) -> None:
        """Validate command parameters using domain validators.

        Each parameter type is validated with the appropriate domain
        validator before being included in the command string.
        """
        for param_name in params:
            if param_name not in expected_params:
                raise ValueError(f"Unexpected parameter {param_name!r} for command {command_key!r}")

        # Apply domain validation based on parameter name
        if "container" in params:
            validate_container_name(str(params["container"]))

        if "service" in params:
            validate_service_name(str(params["service"]))

        if "unit" in params:
            # Journal unit names follow systemd naming rules
            validate_service_name(str(params["unit"]))

        if "tail" in params:
            validate_line_limit(int(params["tail"]))

        if "lines" in params:
            validate_line_limit(int(params["lines"]))

        if "port" in params:
            validate_port(int(params["port"]))

        if "path" in params:
            from pathlib import PurePosixPath
            path_str = str(params["path"])
            if ".." in PurePosixPath(path_str).parts:
                raise ValueError("Path parameters cannot contain parent traversal (..)")

        # Duration parameter for time-based queries
        if "duration" in params:
            validate_duration(str(params["duration"]))

        # Workspace ID validation for workspace-scoped commands
        if "workspace_id" in params:
            validate_workspace_id(str(params["workspace_id"]))

        # Priority validation for journalctl
        if "priority" in params:
            validate_priority(str(params["priority"]))

        # Project ID validation for deployments
        if "project" in params:
            validate_workspace_id(str(params["project"]))

        # Basic format validation for since/until strings (allows relative time like "1 hour ago")
        datetime_re = re.compile(r"^[a-zA-Z0-9_T:., -]+$")
        for date_param in ("since", "until"):
            if date_param in params:
                val = str(params[date_param])
                if not datetime_re.match(val):
                    raise ValueError(f"Invalid format for {date_param}: {val!r}")

    def _construct_command(
        self,
        template: str,
        command_key: str,
        params: dict[str, str | int],
    ) -> str:
        """Construct the final command string from template and parameters.

        Appends validated parameters to the template command.
        Shell-escaping is applied to prevent injection.
        """
        if not params:
            return template

        parts: list[str] = [template]

        # Parameter-specific command construction
        if command_key == "docker_inspect" and "container" in params:
            parts.append(shlex.quote(str(params["container"])))

        elif command_key == "docker_logs":
            if "since" in params:
                parts.extend(["--since", shlex.quote(str(params["since"]))])
            if "until" in params:
                parts.extend(["--until", shlex.quote(str(params["until"]))])
            if "tail" in params:
                parts.extend(["--tail", shlex.quote(str(params["tail"]))])
            if "container" in params:
                parts.append(shlex.quote(str(params["container"])))

        elif command_key in ("docker_stats", "docker_port") and "container" in params:
            parts.append(shlex.quote(str(params["container"])))

        elif command_key == "systemctl_status" and "service" in params:
            parts.append(shlex.quote(str(params["service"])))

        elif command_key == "journalctl":
            if "unit" in params:
                parts.extend(["-u", shlex.quote(str(params["unit"]))])
            if "lines" in params:
                parts.extend(["-n", shlex.quote(str(params["lines"]))])
            if "priority" in params:
                parts.extend(["-p", shlex.quote(str(params["priority"]))])
            if "since" in params:
                parts.extend(["--since", shlex.quote(str(params["since"]))])
            if "until" in params:
                parts.extend(["--until", shlex.quote(str(params["until"]))])

        elif command_key == "compose_logs":
            if "service" in params:
                parts.append(shlex.quote(str(params["service"])))
            if "tail" in params:
                parts.extend(["--tail", shlex.quote(str(params["tail"]))])

        elif command_key in ("stat_size", "readlink") and "path" in params:
            parts.append(shlex.quote(str(params["path"])))

        elif command_key == "tail_file":
            if "lines" in params:
                parts.append(shlex.quote(str(params["lines"])))
            if "path" in params:
                parts.append(shlex.quote(str(params["path"])))

        elif command_key == "workspace_deploy" and "project" in params:
            parts.append(shlex.quote(str(params["project"])))

        return " ".join(parts)
