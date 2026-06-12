"""Network audit tools for MCP OranPi.

Provides read-only inspection of network state, port occupancy,
and Tailscale VPN status on the remote OrangePi host.

All tools follow the audit-first, human-in-the-loop philosophy:
- Observe and report only
- No firewall changes or network configuration
- Constructor DI for all dependencies
"""

from __future__ import annotations

import structlog

from mcp_oranpi.domain.errors import (
    CONN_FAILED,
    CONN_TIMEOUT,
    NET_SCAN_RANGE_INVALID,
    NET_SCAN_TIMEOUT,
    NET_TAILSCALE_NOT_INSTALLED,
    ToolError,
    ToolResult,
)
from mcp_oranpi.domain.validation import validate_port, validate_port_range
from mcp_oranpi.infrastructure.command_runner import CommandRunner
from mcp_oranpi.infrastructure.parsers import parse_ss_tulnp, parse_tailscale_status

log = structlog.get_logger()


class NetworkTools:
    """Network inspection tools for OrangePi host.

    Provides read-only access to network bindings, port occupancy,
    and Tailscale VPN status. All operations are audit-only.

    Args:
        runner: CommandRunner instance for executing remote commands.
    """

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    # ── Port Scanning ─────────────────────────────────────────────────────────

    async def network_scan_ports(
        self,
        range_start: int = 1,
        range_end: int = 1024,
        protocol: str = "tcp",
    ) -> ToolResult:
        """Scan network ports within a range on the remote host.

        Args:
            range_start: First port to check (1-65535).
            range_end: Last port to check (1-65535).
            protocol: Protocol filter (tcp/udp). Default tcp.

        Returns:
            ToolResult with occupied ports and scan metadata.
            Max range is 10000 ports.
        """
        log.info(
            "network_scan_ports",
            range_start=range_start,
            range_end=range_end,
            protocol=protocol,
        )

        try:
            validate_port_range(range_start, range_end)
        except Exception as e:
            return ToolResult(
                error=ToolError(
                    code=NET_SCAN_RANGE_INVALID,
                    message=str(e),
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run("ss_tulnp")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=NET_SCAN_TIMEOUT,
                    message="Port scan timed out",
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

        import time

        start_time = time.monotonic()

        occupied_ports = parse_ss_tulnp(result.stdout)

        # Filter by range and protocol
        filtered = [
            p
            for p in occupied_ports
            if range_start <= p.port <= range_end and p.protocol == protocol
        ]

        end_time = time.monotonic()
        scan_duration_ms = int((end_time - start_time) * 1000)

        port_list = [
            {
                "port": p.port,
                "protocol": p.protocol,
                "process": p.process,
                "container": p.container,
            }
            for p in filtered
        ]

        # Get hostname (best-effort, not critical)
        try:
            hostname_result = await self._runner.run("hostname")
            hostname = hostname_result.stdout.strip()
        except (TimeoutError, ConnectionError):
            hostname = "unknown"

        return ToolResult(
            data={
                "host": hostname,
                "occupied_ports": port_list,
                "scan_duration_ms": scan_duration_ms,
                "truncated": False,
            }
        )

    # ── Port Suggestion ───────────────────────────────────────────────────────

    async def network_suggest_port(
        self,
        preferred_port: int,
        range: int = 100,  # noqa: A002
        count: int = 5,
    ) -> ToolResult:
        """Suggest available ports near a preferred port.

        Args:
            preferred_port: The port the caller would prefer to use.
            range: Search within this many ports above and below preferred.
            count: Return up to this many suggestions.

        Returns:
            ToolResult with preferred port status and suggestions.
        """
        log.info(
            "network_suggest_port",
            preferred_port=preferred_port,
            range=range,
            count=count,
        )

        try:
            validate_port(preferred_port)
        except Exception as e:
            return ToolResult(
                error=ToolError(
                    code="VALID_PARAM_INVALID",
                    message=str(e),
                    retryable=False,
                )
            )

        try:
            result = await self._runner.run("ss_tulnp")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while checking port occupancy",
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

        occupied_ports = parse_ss_tulnp(result.stdout)
        occupied_set = {p.port for p in occupied_ports}

        # Check if preferred is available
        preferred_available = preferred_port not in occupied_set

        # Generate suggestions
        suggestions: list[dict[str, object]] = list()
        candidates_checked = 0
        port = preferred_port

        while len(suggestions) < count and candidates_checked < range * 2:
            # Check port and port - offset first (prefer close ports)
            for offset in [0, -1, 1, -2, 2]:
                check_port = port + offset
                if 1 <= check_port <= 65535 and check_port not in occupied_set:
                    # Find occupant info if any
                    occupant = None
                    for p in occupied_ports:
                        if p.port == check_port:
                            occupant = p.process
                            break

                    suggestions.append(
                        {
                            "port": check_port,
                            "available": True,
                            "occupant": occupant,
                        }
                    )
                    break

            candidates_checked += 1
            port += 1

            if port > 65535:
                port = 1  # Wrap around

            if candidates_checked >= range * 2:
                break

        return ToolResult(
            data={
                "preferred_port": preferred_port,
                "preferred_available": preferred_available,
                "suggestions": suggestions[:count],
            }
        )

    # ── Binding Inspection ────────────────────────────────────────────────────

    async def network_inspect_bindings(self) -> ToolResult:
        """Inspect all active network bindings on the remote host.

        Returns:
            ToolResult with list of NetworkBinding.
        """
        log.info("network_inspect_bindings")

        try:
            result = await self._runner.run("ss_tulnp")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while inspecting bindings",
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

        occupied_ports = parse_ss_tulnp(result.stdout)

        bindings: list[dict[str, object]] = list()
        for p in occupied_ports:
            bindings.append(
                {
                    "local_address": f"0.0.0.0:{p.port}"
                    if not p.process
                    else f"0.0.0.0:{p.port}",
                    "port": p.port,
                    "protocol": p.protocol,
                    "process": p.process,
                    "pid": None,  # ss doesn't always give PID in human-readable
                    "container": p.container,
                }
            )

        return ToolResult(data={"bindings": bindings})

    # ── Tailscale Status ──────────────────────────────────────────────────────

    async def network_tailscale_status(self) -> ToolResult:
        """Get Tailscale VPN status on the remote host.

        Returns:
            ToolResult with TailscaleStatus including peer list.
        """
        log.info("network_tailscale_status")

        try:
            result = await self._runner.run("tailscale_status")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while checking Tailscale status",
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

        # Exit code 127: command not found
        if result.exit_code == 127:
            return ToolResult(
                error=ToolError(
                    code=NET_TAILSCALE_NOT_INSTALLED,
                    message="Tailscale is not installed on this host",
                    detail={
                        "suggestion": (
                            "Install with: "
                            "curl -fsSL https://tailscale.com/install.sh | sh"
                        )
                    },
                    retryable=False,
                )
            )

        if result.exit_code != 0:
            return ToolResult(
                error=ToolError(
                    code=NET_TAILSCALE_NOT_INSTALLED,
                    message=f"tailscale status failed: {result.stderr[:200]}",
                    retryable=False,
                )
            )

        status = parse_tailscale_status(result.stdout)

        return ToolResult(
            data={
                "online": status.online,
                "tailscale_ip": status.tailscale_ip,
                "public_ip": status.public_ip,
                "hostname": status.hostname,
                "dns_name": status.dns_name,
                "peers": [
                    {
                        "name": peer.name,
                        "tailscale_ip": peer.tailscale_ip,
                        "online": peer.online,
                        "last_seen": peer.last_seen,
                    }
                    for peer in status.peers
                ],
            }
        )