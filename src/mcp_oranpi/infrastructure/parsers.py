"""Command output parsers for MCP OranPi.

Pure functions that convert raw SSH command output into domain models.
Parsers NEVER raise application-level errors. For malformed input,
they return empty/default results and log a warning.
"""

from __future__ import annotations

import json
import re
from contextlib import suppress

import structlog

from mcp_oranpi.domain.models import (
    BlockIO,
    ComposeContainer,
    ContainerDetail,
    ContainerStats,
    ContainerSummary,
    CPUUsage,
    DiskInfo,
    DockerLogResult,
    HealthStatus,
    MemoryUsage,
    MountInfo,
    NetworkIO,
    OccupiedPort,
    PortBinding,
    SensorReading,
    ServiceInfo,
    SystemdLogResult,
    TailscalePeer,
    TailscaleStatus,
)

log = structlog.get_logger()


# ── Helper Functions ───────────────────────────────────────────────────────────


def bin_size_to_bytes(size_str: str) -> int:
    """Convert Docker/stats size strings to bytes.

    Handles: B, kB, MB, GB, KiB, MiB, GiB
    Returns integer bytes.
    """
    if not size_str or size_str == "":
        return 0

    size_str = size_str.strip()

    # Pattern: number + optional unit (B, kB, MB, GB or KiB, MiB, GiB)
    match = re.match(r"^([\d.]+)\s*([KMGT]?i?B?)$", size_str, re.IGNORECASE)
    if not match:
        log.warning("parse_error", parser="bin_size_to_bytes", input=size_str)
        return 0

    value_str, unit = match.groups()
    try:
        value = float(value_str)
    except ValueError:
        log.warning("parse_error", parser="bin_size_to_bytes", input=size_str)
        return 0

    # Normalize unit to uppercase without 'i' for binary calculation
    unit = unit.upper().replace("I", "")

    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
    }

    if unit in multipliers:
        return int(value * multipliers[unit])

    log.warning("parse_error", parser="bin_size_to_bytes", input=size_str)
    return 0


def parse_host_port_from_ss(local_addr: str) -> tuple[str, int]:
    """Extract IP and port from ss -tulnp local address.

    Handles formats like:
    - "0.0.0.0:8080"
    - "[::]:8080"
    - "127.0.0.1:3000"

    Returns (ip, port) tuple.
    """
    local_addr = local_addr.strip()

    # Handle IPv6 format with brackets: [::]:8080
    if local_addr.startswith("["):
        match = re.match(r"\[([^\]]+)\]:(\d+)", local_addr)
        if match:
            ip, port_str = match.groups()
            try:
                return (ip, int(port_str))
            except ValueError:
                pass
        return ("", 0)

    # Handle IPv4 format: 0.0.0.0:8080
    if ":" in local_addr:
        parts = local_addr.rsplit(":", 1)
        if len(parts) == 2:
            ip, port_str = parts
            try:
                return (ip, int(port_str))
            except ValueError:
                pass

    return ("", 0)


def _parse_port_binding(port_str: str) -> PortBinding | None:
    """Parse a single Docker port binding string.

    Formats:
    - "0.0.0.0:8080->80/tcp"
    - "[::]:8080->80/tcp"
    - "8080->80/tcp"
    """
    if not port_str or port_str.strip() == "":
        return None

    port_str = port_str.strip()

    # Pattern: [host_ip]:host_port->container_port/proto
    # or host_port->container_port/proto
    match = re.match(
        r"(?:\[([^\]]+)\]|([\d.]+)):(\d+)->(\d+)/(\w+)",
        port_str,
    )
    if match:
        host_ip = match.group(1) or match.group(2) or "0.0.0.0"  # noqa: S104
        host_port = int(match.group(3))
        container_port = int(match.group(4))
        protocol = match.group(5)
        return PortBinding(
            host_port=host_port,
            container_port=container_port,
            protocol=protocol,
            host_ip=host_ip,
        )

    return None


# ── Docker Parsers ─────────────────────────────────────────────────────────────


def parse_docker_ps(stdout: str) -> list[ContainerSummary]:
    """Parse docker ps --format json output.

    Docker outputs one JSON object per line (NOT a JSON array).
    Each line contains: ID, Names, Image, Status, CreatedAt, Ports
    """
    if not stdout or stdout.strip() == "":
        return []

    containers: list[ContainerSummary] = list()

    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            log.warning("parse_error", parser="parse_docker_ps", line=line[:100])
            continue

        # Parse ports field - can be empty string or comma-separated bindings
        ports: list[PortBinding] = list()
        ports_str = data.get("Ports", "")
        if ports_str and isinstance(ports_str, str):
            for binding_str in ports_str.split(", "):
                binding_str = binding_str.strip()
                if binding_str:
                    binding = _parse_port_binding(binding_str)
                    if binding:
                        ports.append(binding)

        try:
            container = ContainerSummary(
                id=data.get("ID", "")[:12],  # Short ID
                name=data.get("Names", ""),
                image=data.get("Image", ""),
                status=data.get("Status", ""),
                created=data.get("CreatedAt", ""),
                ports=ports,
            )
            containers.append(container)
        except Exception as e:
            log.warning(
                "parse_error",
                parser="parse_docker_ps",
                error=str(e),
                data=str(data)[:100],
            )
            continue

    return containers


def parse_docker_inspect(stdout: str) -> ContainerDetail | None:
    """Parse docker inspect --format json output.

    Returns a JSON array, take first element.
    Extracts: Id, Name, Config/Image, State/Status, Created,
    NetworkSettings, Mounts, Config/Env, Config/Labels
    """
    if not stdout or stdout.strip() == "":
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("parse_error", parser="parse_docker_inspect", output=stdout[:100])
        return None

    if not isinstance(data, list) or len(data) == 0:
        return None

    container = data[0]

    # Parse ports from NetworkSettings
    ports: list[PortBinding] = list()
    networks = container.get("NetworkSettings", {}).get("Networks", {})
    port_bindings = container.get("NetworkSettings", {}).get("Ports", {})

    for container_port, host_bindings in port_bindings.items():
        if not host_bindings:
            continue

        # container_port format: "80/tcp"
        parts = container_port.split("/")
        if len(parts) != 2:
            continue
        container_port_num, protocol = parts

        for binding in host_bindings:
            if binding:
                host_ip = binding.get("HostIp", "0.0.0.0")  # noqa: S104
                host_port = binding.get("HostPort", "")
                ports.append(
                    PortBinding(
                        host_port=int(host_port) if host_port else 0,
                        container_port=int(container_port_num),
                        protocol=protocol,
                        host_ip=host_ip,
                    )
                )

    # Parse mounts
    mounts: list[MountInfo] = list()
    for mount in container.get("Mounts", []):
        try:
            mounts.append(
                MountInfo(
                    source=mount.get("Source", ""),
                    destination=mount.get("Destination", ""),
                    mode=mount.get("Mode", ""),
                    type=mount.get("Type", ""),
                )
            )
        except Exception as e:
            log.warning(
                "parse_error",
                parser="parse_docker_inspect_mount",
                error=str(e),
            )

    # Parse env - extract NAMES only (before = sign), never values
    env_list: list[str] = list()
    for env_entry in container.get("Config", {}).get("Env", []):
        if "=" in env_entry:
            env_name = env_entry.split("=", 1)[0]
            env_list.append(env_name)
        else:
            env_list.append(env_entry)

    # Parse health status
    health = None
    state = container.get("State", {})
    health_data = state.get("Health", {})
    if health_data:
        try:
            health = HealthStatus(
                status=health_data.get("Status", ""),
                failing_streak=health_data.get("FailingStreak", 0),
                last_output=health_data.get("Log", [{}])[-1].get("Output", "")
                if health_data.get("Log")
                else "",
            )
        except Exception as e:
            log.warning(
                "parse_error",
                parser="parse_docker_inspect_health",
                error=str(e),
            )

    # Parse restart policy
    restart_policy = ""
    rp = container.get("HostConfig", {}).get("RestartPolicy", {})
    if rp:
        restart_policy = rp.get("Name", "")

    # Extract network names
    network_names = list(networks.keys())

    try:
        return ContainerDetail(
            id=container.get("Id", ""),
            name=container.get("Name", "").lstrip("/"),
            image=container.get("Config", {}).get("Image", ""),
            status=state.get("Status", ""),
            created=container.get("Created", ""),
            ports=ports,
            networks=network_names,
            labels=container.get("Config", {}).get("Labels", {}),
            env=env_list,
            mounts=mounts,
            health=health,
            restart_policy=restart_policy,
        )
    except Exception as e:
        log.warning(
            "parse_error",
            parser="parse_docker_inspect",
            error=str(e),
            container_id=container.get("Id", "")[:12],
        )
        return None


def parse_docker_stats(stdout: str) -> ContainerStats | None:
    """Parse docker stats --no-stream --format json output.

    Single JSON line with: Container, CPUPerc, MemUsage, MemPerc,
    NetIO, BlockIO, Pids
    """
    if not stdout or stdout.strip() == "":
        return None

    try:
        data = json.loads(stdout.strip())
    except json.JSONDecodeError:
        log.warning("parse_error", parser="parse_docker_stats", output=stdout[:100])
        return None

    # Parse CPU percentage - strip % and convert
    cpu_percent = 0.0
    cpu_str = data.get("CPUPerc", "0%")
    try:
        cpu_percent = float(cpu_str.rstrip("%"))
    except ValueError:
        log.warning(
            "parse_error",
            parser="parse_docker_stats_cpu",
            value=cpu_str,
        )

    # Parse memory "12.34MiB / 512MiB"
    memory_usage_mb = 0.0
    memory_limit_mb = 0.0
    memory_percent = 0.0

    mem_str = data.get("MemUsage", "0B / 0B")
    mem_match = re.match(r"([\d.]+)\s*(\w+)\s*/\s*([\d.]+)\s*(\w+)", mem_str)
    if mem_match:
        usage_val, usage_unit, limit_val, limit_unit = mem_match.groups()
        try:
            memory_usage_mb = _mem_to_mb(float(usage_val), usage_unit)
            memory_limit_mb = _mem_to_mb(float(limit_val), limit_unit)
        except ValueError:
            pass

    mem_perc_str = data.get("MemPerc", "0%")
    with suppress(ValueError):
        memory_percent = float(mem_perc_str.rstrip("%"))

    # Parse network IO "1.2kB / 3.4kB" -> (in, out)
    network_io = None
    net_str = data.get("NetIO", "0B / 0B")
    net_match = re.match(r"([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+)", net_str)
    if net_match:
        net_in = bin_size_to_bytes(net_match.group(1))
        net_out = bin_size_to_bytes(net_match.group(2))
        network_io = NetworkIO(bytes_in=net_in, bytes_out=net_out)

    # Parse block IO "5.6MB / 7.8MB" -> (read, write)
    block_io = None
    block_str = data.get("BlockIO", "0B / 0B")
    block_match = re.match(r"([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+)", block_str)
    if block_match:
        block_read = bin_size_to_bytes(block_match.group(1))
        block_write = bin_size_to_bytes(block_match.group(2))
        block_io = BlockIO(bytes_read=block_read, bytes_written=block_write)

    # Parse pids
    pids = 0
    with suppress(ValueError):
        pids = int(data.get("Pids", 0))

    try:
        return ContainerStats(
            container=data.get("Container", ""),
            cpu_percent=cpu_percent,
            memory_usage_mb=memory_usage_mb,
            memory_limit_mb=memory_limit_mb,
            memory_percent=memory_percent,
            network_io=network_io,
            block_io=block_io,
            pids=pids,
        )
    except Exception as e:
        log.warning(
            "parse_error",
            parser="parse_docker_stats",
            error=str(e),
        )
        return None


def _mem_to_mb(value: float, unit: str) -> float:
    """Convert memory value to megabytes."""
    unit = unit.upper().replace("I", "")
    multipliers = {
        "B": 1 / (1024**2),
        "KB": 1024 / (1024**2),
        "MB": 1,
        "GB": 1024,
        "TB": 1024**2,
    }
    if unit in multipliers:
        return value * multipliers[unit]
    return value


def parse_docker_port(stdout: str) -> list[PortBinding]:
    """Parse docker port <container> output.

    Format: "80/tcp -> 0.0.0.0:8080" or "80/tcp -> [::]:8080"
    """
    if not stdout or stdout.strip() == "":
        return []

    bindings: list[PortBinding] = list()

    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # Parse "80/tcp -> 0.0.0.0:8080"
        match = re.match(r"(\d+)/(\w+)\s*->\s*(.+)", line)
        if match:
            container_port = int(match.group(1))
            protocol = match.group(2)
            host_part = match.group(3)

            # Parse host address
            host_binding = _parse_port_binding(f"{host_part}->{container_port}/{protocol}")
            if host_binding:
                bindings.append(
                    PortBinding(
                        host_port=host_binding.host_port,
                        container_port=container_port,
                        protocol=protocol,
                        host_ip=host_binding.host_ip,
                    )
                )
        else:
            log.warning("parse_error", parser="parse_docker_port", line=line)

    return bindings


# ── Network Parsers ────────────────────────────────────────────────────────────


def parse_ss_tulnp(stdout: str) -> list[OccupiedPort]:
    """Parse ss -tulnp output.

    Header line: "Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process"
    Lines like: tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1234,fd=6))
    """
    if not stdout or stdout.strip() == "":
        return []

    ports: list[OccupiedPort] = list()
    lines = stdout.strip().split("\n")

    # Skip header line
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue

        # Split by whitespace - columns are space-separated
        parts = line.split()
        if len(parts) < 5:
            continue

        netid = parts[0]
        state = parts[1]

        # Only LISTEN state
        if state != "LISTEN":
            continue

        # Local Address:Port is in parts[4]
        local_addr = parts[4]

        ip, port = parse_host_port_from_ss(local_addr)
        if port == 0:
            continue

        # Extract process info from the Process column (last column)
        # Format: users:(("name",pid=1234,fd=6))
        process_name = None
        if len(parts) >= 6:
            process_part = " ".join(parts[5:])
            proc_match = re.search(r'users:\(\("([^"]+)"', process_part)
            if proc_match:
                process_name = proc_match.group(1)

        # If process contains "docker-proxy", set container to None
        # (detected via docker port mapping separately)
        container: str | None = None
        if process_name and "docker-proxy" in process_name.lower():
            container = None

        ports.append(
            OccupiedPort(
                port=port,
                protocol=netid,
                process=process_name,
                container=container,
            )
        )

    return ports


def parse_tailscale_status(stdout: str) -> TailscaleStatus:
    """Parse tailscale status --json output.

    Full JSON with: Self.Online, Self.TailscaleIPs, Self.DNSName, Peers
    """
    if not stdout or stdout.strip() == "":
        return TailscaleStatus(online=False)

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("parse_error", parser="parse_tailscale_status", output=stdout[:100])
        return TailscaleStatus(online=False)

    self_info = data.get("Self", {})
    online = self_info.get("Online", False)
    tailscale_ips = self_info.get("TailscaleIPs", [])
    dns_name = self_info.get("DNSName", "")

    # Tailscale IPs are typically [IPv4, IPv6]
    tailscale_ip = tailscale_ips[0] if tailscale_ips else None

    # Parse peers
    peers: list[TailscalePeer] = list()
    peers_data = data.get("Peers", {})
    for peer_id, peer_info in peers_data.items():
        try:
            peer = TailscalePeer(
                name=peer_info.get("HostName", peer_id),
                tailscale_ip=peer_info.get("TailscaleIPs", [""])[0],
                online=peer_info.get("Online", False),
                last_seen=peer_info.get("LastSeen", None),
            )
            peers.append(peer)
        except Exception as e:
            log.warning(
                "parse_error",
                parser="parse_tailscale_status_peer",
                peer_id=peer_id,
                error=str(e),
            )

    return TailscaleStatus(
        online=online,
        tailscale_ip=tailscale_ip,
        dns_name=dns_name,
        peers=peers,
    )


# ── System Parsers ─────────────────────────────────────────────────────────────


def parse_top_bn1(stdout: str) -> CPUUsage:
    """Parse top -bn1 output.

    v0.1: only global CPU average. per_core is a single-element list.
    Extracts:
    - %Cpu(s): line like "3.2 us, 1.1 sy, 0.0 ni, 95.2 id, 0.3 wa, 0.0 hi, 0.2 si"
    - load average: "0.10, 0.15, 0.20"
    - uptime: "up 42 days, 3:15"
    """
    cpu_percent = 0.0
    per_core = [0.0]
    load_avg_1m = 0.0
    load_avg_5m = 0.0
    load_avg_15m = 0.0
    uptime_seconds = 0

    for line in stdout.split("\n"):
        line = line.strip()

        # %Cpu(s) line - calculate CPU usage as 100 - idle
        if "%Cpu(s):" in line or "%Cpu" in line:
            # Find idle value
            match = re.search(r"(\d+\.?\d*)\s*id", line)
            if match:
                idle = float(match.group(1))
                cpu_percent = 100.0 - idle

                # Extract individual core percentages if available
                us_match = re.search(r"(\d+\.?\d*)\s*us", line)
                sy_match = re.search(r"(\d+\.?\d*)\s*sy", line)
                if us_match and sy_match:
                    # For now, just store the total as per_core[0]
                    per_core = [cpu_percent]

        # Load average line
        if "load average:" in line.lower():
            load_matches = re.findall(r"(\d+\.?\d+)", line)
            if len(load_matches) >= 3:
                try:
                    load_avg_1m = float(load_matches[0])
                    load_avg_5m = float(load_matches[1])
                    load_avg_15m = float(load_matches[2])
                except ValueError:
                    pass

        # Uptime line
        if line.startswith("up "):
            uptime_seconds = _parse_uptime(line)

    return CPUUsage(
        cpu_percent=cpu_percent,
        per_core=per_core,
        load_avg_1m=load_avg_1m,
        load_avg_5m=load_avg_5m,
        load_avg_15m=load_avg_15m,
        uptime_seconds=uptime_seconds,
    )


def _parse_uptime(line: str) -> int:
    """Parse uptime string to seconds.

    Examples:
    "up 42 days, 3:15"
    "up 1 hour, 5 min"
    "up 3:45"
    """
    total_seconds = 0

    # Extract numbers and units
    days_match = re.search(r"(\d+)\s*day", line)
    if days_match:
        total_seconds += int(days_match.group(1)) * 86400

    hours_min_match = re.search(r"(\d+):(\d+)", line)
    if hours_min_match:
        hours = int(hours_min_match.group(1))
        minutes = int(hours_min_match.group(2))
        total_seconds += hours * 3600 + minutes * 60

    # Handle "X min" format
    min_match = re.search(r"(\d+)\s*min", line)
    if min_match:
        total_seconds += int(min_match.group(1)) * 60

    # Handle "X hour" (without minutes)
    hour_only_match = re.search(r"(\d+)\s*hour", line)
    if hour_only_match and not hours_min_match:
        total_seconds += int(hour_only_match.group(1)) * 3600

    return total_seconds


def parse_free_m(stdout: str) -> MemoryUsage:
    """Parse free -m output.

    Lines: header, Mem: row, Swap: row
    Mem row: total, used, free, shared, buff/cache, available
    Swap row: total, used, free
    """
    mem_total = 0.0
    mem_used = 0.0
    mem_available = 0.0
    mem_percent = 0.0
    swap_total = 0.0
    swap_used = 0.0
    swap_percent = 0.0

    lines = stdout.strip().split("\n")

    for line in lines:
        line = line.strip()
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                try:
                    mem_total = float(parts[1])
                    mem_used = float(parts[2])
                    mem_available = float(parts[6])
                    if mem_total > 0:
                        mem_percent = (mem_used / mem_total) * 100
                except (ValueError, IndexError):
                    pass

        elif line.startswith("Swap:"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    swap_total = float(parts[1])
                    swap_used = float(parts[2])
                    if swap_total > 0:
                        swap_percent = (swap_used / swap_total) * 100
                except (ValueError, IndexError):
                    pass

    return MemoryUsage(
        total_mb=mem_total,
        used_mb=mem_used,
        available_mb=mem_available,
        percent=mem_percent,
        swap_total_mb=swap_total,
        swap_used_mb=swap_used,
        swap_percent=swap_percent,
    )


def parse_df_t(stdout: str) -> list[DiskInfo]:
    """Parse df -T output (NOT df -h).

    Columns: Filesystem Type 1K-blocks Used Available Use% Mounted on
    Convert 1K-blocks to GB (divide by 1024*1024)
    Skip tmpfs, devtmpfs, squashfs entries
    """
    if not stdout or stdout.strip() == "":
        return []

    disks: list[DiskInfo] = list()
    lines = stdout.strip().split("\n")

    # Skip header line
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 7:
            continue

        filesystem = parts[0]
        fs_type = parts[1]

        # Skip pseudo filesystems
        if fs_type in ("tmpfs", "devtmpfs", "squashfs", "overlay"):
            continue

        try:
            total_1k = float(parts[2])
            used = float(parts[3])
            available = float(parts[4])
            use_percent = float(parts[5].rstrip("%"))
            mount_point = parts[6]

            # Convert 1K-blocks to GB
            total_gb = total_1k / (1024 * 1024)
            used_gb = used / (1024 * 1024)
            available_gb = available / (1024 * 1024)

            disks.append(
                DiskInfo(
                    mount_point=mount_point,
                    device=filesystem,
                    total_gb=round(total_gb, 2),
                    used_gb=round(used_gb, 2),
                    available_gb=round(available_gb, 2),
                    percent=use_percent,
                    filesystem_type=fs_type,
                )
            )
        except (ValueError, IndexError) as e:
            log.warning(
                "parse_error",
                parser="parse_df_t",
                line=line[:100],
                error=str(e),
            )
            continue

    return disks


def parse_systemctl_list(stdout: str) -> list[ServiceInfo]:
    """Parse systemctl list-units --type=service output.

    Skip header line, parse unit name, active state, sub-state.
    """
    if not stdout or stdout.strip() == "":
        return []

    services: list[ServiceInfo] = list()
    lines = stdout.strip().split("\n")

    # Skip header lines (typically 2 lines: header and separator)
    skip_count = 0
    for line in lines:
        if skip_count < 2 or line.startswith(" UNIT") or re.match(r"^[-=\s]+$", line):
            skip_count += 1
            continue
        break

    for line in lines[skip_count:]:
        line = line.strip()
        if not line:
            continue

        # Parse service line - format is flexible but typically:
        # UNIT LOAD ACTIVE SUB DESCRIPTION
        parts = line.split()
        if len(parts) < 4:
            continue

        name = parts[0]
        # Remove .service suffix if present for consistency
        if name.endswith(".service"):
            name = name[:-8]

        active = parts[2]
        sub_state = parts[3]

        # Description is everything after SUB
        description = " ".join(parts[4:]) if len(parts) > 4 else ""

        services.append(
            ServiceInfo(
                name=name,
                active=active,
                sub_state=sub_state,
                enabled=True,  # Would need systemctl is-enabled to determine
                description=description,
            )
        )

    return services


def parse_systemctl_status(stdout: str) -> ServiceInfo | None:
    """Parse systemctl status <service> output for a single service."""
    if not stdout or stdout.strip() == "":
        return None

    lines = stdout.strip().split("\n")
    if not lines:
        return None

    # First line contains the service name and basic state
    # e.g. "nginx.service - A nginx web server"
    first_line = lines[0]

    # Extract name from first line
    name_match = re.match(r"^([\w.-]+)\.service", first_line)
    name = name_match.group(1) if name_match else ""

    # Active state is typically in parentheses
    active_match = re.search(r"\(([^)]+)\)", first_line)
    active = active_match.group(1) if active_match else ""

    # Sub state is often on the same line after active
    # Format varies, so be defensive
    sub_state = ""
    if ";" in first_line:
        sub_parts = first_line.split(";")
        if len(sub_parts) > 1:
            sub_state = sub_parts[1].strip().split()[0] if sub_parts[1].strip() else ""

    # Try to extract description from first line after the dash
    description = ""
    if " - " in first_line:
        desc_match = re.search(r"-\s*(.+)$", first_line)
        if desc_match:
            description = desc_match.group(1).strip()

    if not name:
        return None

    return ServiceInfo(
        name=name,
        active=active,
        sub_state=sub_state,
        enabled=True,  # Would need separate check
        description=description,
    )


def parse_vcgencmd_temp(stdout: str) -> list[SensorReading]:
    """Parse vcgencmd measure_temp output.

    Output like: temp=48.3'C
    Returns list with single SensorReading(name="cpu", temp_c=48.3)
    """
    if not stdout or stdout.strip() == "":
        return []

    # Extract temperature value
    match = re.search(r"temp=([\d.]+)'C", stdout)
    if match:
        try:
            temp = float(match.group(1))
            return [SensorReading(name="cpu", temp_c=temp)]
        except ValueError:
            pass

    log.warning("parse_error", parser="parse_vcgencmd_temp", output=stdout[:50])
    return []


# ── Log Parsers ────────────────────────────────────────────────────────────────


def parse_journalctl(stdout: str) -> SystemdLogResult:
    """Wrap raw journalctl output.

    Counts newlines, returns SystemdLogResult.
    Unit comes from the tool call, not the output.
    """
    if not stdout:
        return SystemdLogResult(unit=None, log_count=0, logs="")

    logs = stdout
    log_count = len(stdout.split("\n")) if stdout.strip() else 0

    return SystemdLogResult(
        unit=None,  # Set by caller
        log_count=log_count,
        logs=logs,
    )


def parse_docker_logs(stdout: str, container: str) -> DockerLogResult:
    """Wrap raw docker logs output.

    Counts lines, returns DockerLogResult.
    Container name comes from the tool call.
    """
    if not stdout:
        return DockerLogResult(container=container, log_count=0, logs="")

    logs = stdout
    log_count = len(stdout.split("\n")) if stdout.strip() else 0

    return DockerLogResult(
        container=container,
        log_count=log_count,
        logs=logs,
    )


# ── File Parser ────────────────────────────────────────────────────────────────


def parse_stat_size(stdout: str) -> int:
    """Parse stat -c %s <path> output.

    Just an integer (file size in bytes).
    Returns 0 if parsing fails.
    """
    if not stdout or stdout.strip() == "":
        return 0

    try:
        return int(stdout.strip())
    except ValueError:
        log.warning("parse_error", parser="parse_stat_size", output=stdout[:50])
        return 0


# ── Compose Parser ─────────────────────────────────────────────────────────────


def parse_compose_ps(stdout: str) -> list[ComposeContainer]:
    """Parse docker compose ps --format json output.

    One JSON object per line with: Name, Service, State, Health, Publishers
    Publishers is a list of port mappings.
    """
    if not stdout or stdout.strip() == "":
        return []

    containers: list[ComposeContainer] = list()

    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            log.warning("parse_error", parser="parse_compose_ps", line=line[:100])
            continue

        # Parse ports from Publishers
        ports: list[PortBinding] = list()
        publishers = data.get("Publishers", [])
        if publishers and isinstance(publishers, list):
            for pub in publishers:
                if isinstance(pub, dict):
                    ports.append(
                        PortBinding(
                            host_port=pub.get("PublishedPort", 0),
                            container_port=pub.get("TargetPort", 0),
                            protocol=pub.get("Protocol", "tcp"),
                            host_ip=pub.get("BindIP", "0.0.0.0"),  # noqa: S104
                        )
                    )

        try:
            container = ComposeContainer(
                name=data.get("Name", ""),
                service=data.get("Service", ""),
                state=data.get("State", ""),
                health=data.get("Health", None),
                ports=ports,
                created=data.get("Created", ""),
            )
            containers.append(container)
        except Exception as e:
            log.warning(
                "parse_error",
                parser="parse_compose_ps",
                error=str(e),
                data=str(data)[:100],
            )
            continue

    return containers