"""System audit tools for MCP OranPi.

Provides read-only inspection of CPU, memory, disk, temperatures,
and systemd services on the remote OrangePi host.

All tools follow the audit-first, human-in-the-loop philosophy:
- Observe and report only
- No service manipulation or system configuration
- Constructor DI for all dependencies
"""

from __future__ import annotations

import structlog

from mcp_oranpi.application.parsers import (
    parse_df_t,
    parse_free_m,
    parse_systemctl_list,
    parse_systemctl_status,
    parse_thermal_zones,
    parse_top_bn1,
    parse_vcgencmd_temp,
)
from mcp_oranpi.domain.contracts import CommandRunnerProtocol
from mcp_oranpi.domain.errors import (
    CONN_FAILED,
    CONN_TIMEOUT,
    SYS_SENSORS_UNAVAILABLE,
    SYS_SERVICE_NOT_FOUND,
    ToolError,
    ToolResult,
)
from mcp_oranpi.domain.validation import validate_service_name

log = structlog.get_logger()


class SystemTools:
    """System inspection tools for OrangePi host.

    Provides read-only access to CPU, memory, disk, temperature sensors,
    and systemd service state. All operations are audit-only.

    Args:
        runner: CommandRunner instance for executing remote commands.
    """

    def __init__(self, runner: CommandRunnerProtocol) -> None:
        self._runner = runner

    # ── CPU Usage ─────────────────────────────────────────────────────────────

    async def system_cpu_usage(self, duration: str = "5m") -> ToolResult:
        """Get CPU usage snapshot from the remote host.

        Args:
            duration: Duration literal (accepted but IGNORED in v0.1).
                     v0.1 always returns a single snapshot, not historical.

        Returns:
            ToolResult with CPUUsage. Per-core is single-element list in v0.1.
        """
        log.info("system_cpu_usage", duration=duration)

        try:
            result = await self._runner.run("top_bn1")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching CPU usage",
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
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message=f"top command failed: {result.stderr[:200]}",
                    retryable=False,
                )
            )

        usage = parse_top_bn1(result.stdout)

        return ToolResult(
            data={
                "cpu_percent": usage.cpu_percent,
                "per_core": usage.per_core,
                "load_avg_1m": usage.load_avg_1m,
                "load_avg_5m": usage.load_avg_5m,
                "load_avg_15m": usage.load_avg_15m,
                "uptime_seconds": usage.uptime_seconds,
            }
        )

    # ── Memory Usage ──────────────────────────────────────────────────────────

    async def system_memory_usage(self) -> ToolResult:
        """Get memory usage snapshot from the remote host.

        Returns:
            ToolResult with MemoryUsage.
        """
        log.info("system_memory_usage")

        try:
            result = await self._runner.run("free_m")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching memory usage",
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
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message=f"free command failed: {result.stderr[:200]}",
                    retryable=False,
                )
            )

        usage = parse_free_m(result.stdout)

        return ToolResult(
            data={
                "total_mb": usage.total_mb,
                "used_mb": usage.used_mb,
                "available_mb": usage.available_mb,
                "percent": usage.percent,
                "swap_total_mb": usage.swap_total_mb,
                "swap_used_mb": usage.swap_used_mb,
                "swap_percent": usage.swap_percent,
            }
        )

    # ── Disk Usage ────────────────────────────────────────────────────────────

    async def system_disk_usage(self, path: str | None = None) -> ToolResult:
        """Get disk usage for mounted filesystems.

        Args:
            path: Optional mount point path to filter results.

        Returns:
            ToolResult with list of DiskInfo.
        """
        log.info("system_disk_usage", path=path)

        try:
            result = await self._runner.run("df_t")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while fetching disk usage",
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
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message=f"df command failed: {result.stderr[:200]}",
                    retryable=False,
                )
            )

        disks = parse_df_t(result.stdout)

        # Filter by path if specified
        if path:
            disks = [d for d in disks if d.mount_point == path]

        disk_dicts = [
            {
                "mount_point": d.mount_point,
                "device": d.device,
                "total_gb": d.total_gb,
                "used_gb": d.used_gb,
                "available_gb": d.available_gb,
                "percent": d.percent,
                "filesystem_type": d.filesystem_type,
            }
            for d in disks
        ]

        return ToolResult(data={"disks": disk_dicts})

    # ── Temperature Sensors ───────────────────────────────────────────────────

    async def system_temperatures(self) -> ToolResult:
        """Get hardware temperature sensor readings.

        Tries Linux thermal zones first (works on all ARM boards including
        OrangePi 5 Plus), falls back to vcgencmd (Raspberry Pi only).

        Returns:
            ToolResult with list of SensorReading.
        """
        log.info("system_temperatures")

        # Strategy 1: Read Linux thermal zones (standard on all ARM boards)
        try:
            result = await self._runner.run("thermal_zones")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while reading temperature sensors",
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

        if result.exit_code == 0 and result.stdout.strip():
            readings = parse_thermal_zones(result.stdout)
            if readings:
                return ToolResult(
                    data={
                        "temperatures": [
                            {
                                "name": r.name,
                                "temp_c": r.temp_c,
                                "critical_c": r.critical_c,
                            }
                            for r in readings
                        ],
                        "source": "thermal_zones",
                        "throttled": False,
                    }
                )

        # Strategy 2: Fallback to vcgencmd (Raspberry Pi only)
        try:
            result = await self._runner.run("vcgencmd_measure_temp")
        except TimeoutError:
            return ToolResult(
                error=ToolError(
                    code=CONN_TIMEOUT,
                    message="Command timed out while reading temperature sensors",
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

        # Exit code 127: vcgencmd not available (not a Raspberry Pi)
        if result.exit_code == 127:
            return ToolResult(
                error=ToolError(
                    code=SYS_SENSORS_UNAVAILABLE,
                    message=(
                        "Temperature sensors not available: "
                        "no thermal zones found and vcgencmd not installed"
                    ),
                    detail={
                        "suggestion": (
                            "Ensure /sys/class/thermal/ is accessible or "
                            "install vcgencmd for Raspberry Pi boards"
                        )
                    },
                    retryable=False,
                )
            )

        if result.exit_code != 0:
            return ToolResult(
                error=ToolError(
                    code=SYS_SENSORS_UNAVAILABLE,
                    message=f"Temperature read failed: {result.stderr[:200]}",
                    retryable=False,
                )
            )

        readings = parse_vcgencmd_temp(result.stdout)
        if not readings:
            return ToolResult(
                error=ToolError(
                    code=SYS_SENSORS_UNAVAILABLE,
                    message="Could not parse temperature sensor output",
                    retryable=False,
                )
            )

        return ToolResult(
            data={
                "temperatures": [
                    {
                        "name": r.name,
                        "temp_c": r.temp_c,
                        "critical_c": r.critical_c,
                    }
                    for r in readings
                ],
                "source": "vcgencmd",
                "throttled": False,
            }
        )

    # ── Service Status ────────────────────────────────────────────────────────

    async def system_service_status(
        self,
        service: str | None = None,
    ) -> ToolResult:
        """Get systemd service status.

        Args:
            service: Optional specific service name. If omitted, lists all.

        Returns:
            ToolResult with list of ServiceInfo.
        """
        log.info("system_service_status", service=service)

        if service:
            try:
                validate_service_name(service)
            except Exception as e:
                return ToolResult(
                    error=ToolError(
                        code="VALID_PARAM_INVALID",
                        message=str(e),
                        retryable=False,
                    )
                )

            try:
                result = await self._runner.run("systemctl_status", service=service)
            except TimeoutError:
                return ToolResult(
                    error=ToolError(
                        code=CONN_TIMEOUT,
                        message="Command timed out while checking service status",
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

            # Exit code 3/4: service not found
            if result.exit_code in (3, 4):
                return ToolResult(
                    error=ToolError(
                        code=SYS_SERVICE_NOT_FOUND,
                        message=f"Service not found: {service}",
                        detail={"service": service},
                        retryable=False,
                    )
                )

            if result.exit_code != 0:
                return ToolResult(
                    error=ToolError(
                        code=SYS_SERVICE_NOT_FOUND,
                        message=f"systemctl status failed: {result.stderr[:200]}",
                        detail={"service": service},
                        retryable=False,
                    )
                )

            info = parse_systemctl_status(result.stdout)

            if info is None:
                return ToolResult(
                    error=ToolError(
                        code=SYS_SERVICE_NOT_FOUND,
                        message=f"Could not parse service status: {service}",
                        detail={"service": service},
                        retryable=False,
                    )
                )

            return ToolResult(
                data={
                    "services": [
                        {
                            "name": info.name,
                            "active": info.active,
                            "sub_state": info.sub_state,
                            "enabled": info.enabled,
                            "description": info.description,
                        }
                    ]
                }
            )
        else:
            # List all services
            try:
                result = await self._runner.run("systemctl_list")
            except TimeoutError:
                return ToolResult(
                    error=ToolError(
                        code=CONN_TIMEOUT,
                        message="Command timed out while listing services",
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
                return ToolResult(
                    error=ToolError(
                        code=SYS_SERVICE_NOT_FOUND,
                        message=f"systemctl list failed: {result.stderr[:200]}",
                        retryable=False,
                    )
                )

            services = parse_systemctl_list(result.stdout)

            return ToolResult(
                data={
                    "services": [
                        {
                            "name": s.name,
                            "active": s.active,
                            "sub_state": s.sub_state,
                            "enabled": s.enabled,
                            "description": s.description,
                        }
                        for s in services
                    ]
                }
            )
