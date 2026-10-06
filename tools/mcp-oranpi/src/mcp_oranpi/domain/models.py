"""Domain models for MCP OranPi.

These are pure data models with NO external dependencies.
Infrastructure concerns (SSH, MCP) are NOT represented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Docker Models ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PortBinding:
    """A single port mapping from host to container."""

    host_port: int
    container_port: int
    protocol: str
    host_ip: str


@dataclass(slots=True)
class ContainerSummary:
    """Summary of a Docker container."""

    id: str
    name: str
    image: str
    status: str
    created: str
    ports: list[PortBinding] = field(default_factory=list)


@dataclass(slots=True)
class ContainerDetail:
    """Detailed inspection of a Docker container.

    Returned by ``docker_inspect_container``. Includes full metadata
    not available in the summary listing.
    """

    id: str
    name: str
    image: str
    status: str
    created: str
    ports: list[PortBinding] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    env: list[str] = field(default_factory=list)
    mounts: list[MountInfo] = field(default_factory=list)
    health: HealthStatus | None = None
    restart_policy: str = ""


@dataclass(slots=True)
class PortOccupant:
    """A port occupied by a Docker container on the host.

    Returned by ``docker_inspect_ports``. Distinct from
    ``OccupiedPort`` which represents any process holding a port.
    """

    host_port: int
    container_port: int
    protocol: str
    container_name: str
    container_id: str
    host_ip: str
    process: str | None = None


@dataclass(slots=True)
class HealthStatus:
    """Container health check result."""

    status: str
    failing_streak: int
    last_output: str


@dataclass(slots=True)
class MountInfo:
    """A container mount (bind or volume)."""

    source: str
    destination: str
    mode: str
    type: str


@dataclass(slots=True)
class NetworkIO:
    """Network IO statistics for a container."""

    bytes_in: int
    bytes_out: int


@dataclass(slots=True)
class BlockIO:
    """Block IO statistics for a container."""

    bytes_read: int
    bytes_written: int


# ── Network Models ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class OccupiedPort:
    """A port in use on the remote host.

    ``local_address`` is the exact ``Local Address:Port`` string reported by
    ``ss`` (for example ``100.106.85.109:8003`` or ``[::]:7474``). It is never
    inferred or rewritten, so callers see the real OS socket binding.
    """

    port: int
    protocol: str
    local_address: str = ""
    process: str | None = None
    pid: int | None = None
    container: str | None = None


@dataclass(slots=True)
class PortSuggestion:
    """A suggested available port near a desired port."""

    port: int
    available: bool
    occupant: str | None = None


@dataclass(slots=True)
class NetworkBinding:
    """An active network binding on the remote host."""

    local_address: str
    port: int
    protocol: str
    process: str | None = None
    pid: int | None = None
    container: str | None = None


@dataclass(slots=True)
class TailscalePeer:
    """A Tailscale mesh peer."""

    name: str
    tailscale_ip: str
    online: bool
    last_seen: str | None = None


@dataclass(slots=True)
class TailscaleStatus:
    """Tailscale VPN status on the remote host.

    Returned by ``network_tailscale_status``.
    """

    online: bool
    tailscale_ip: str | None = None
    public_ip: str | None = None
    hostname: str = ""
    dns_name: str | None = None
    peers: list[TailscalePeer] = field(default_factory=list)


# ── System Models ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DiskInfo:
    """Disk usage for a mounted filesystem."""

    mount_point: str
    device: str
    total_gb: float
    used_gb: float
    available_gb: float
    percent: float
    filesystem_type: str


@dataclass(slots=True)
class SensorReading:
    """A hardware temperature sensor reading."""

    name: str
    temp_c: float
    critical_c: float | None = None


@dataclass(slots=True)
class ServiceInfo:
    """A systemd service status entry."""

    name: str
    active: str
    sub_state: str
    enabled: bool
    description: str


@dataclass(slots=True)
class ContainerStats:
    """Resource usage statistics for a single Docker container.

    Returned by ``docker_container_stats``.
    """

    container: str
    cpu_percent: float
    memory_usage_mb: float
    memory_limit_mb: float
    memory_percent: float
    network_io: NetworkIO | None = None
    block_io: BlockIO | None = None
    pids: int = 0


@dataclass(slots=True)
class ComposeContainer:
    """A container within a Docker Compose project.

    Returned by ``workspace_docker_ps`` and ``workspace_ports``.
    """

    name: str
    service: str
    state: str
    health: str | None = None
    ports: list[PortBinding] = field(default_factory=list)
    created: str = ""


@dataclass(slots=True)
class WorkspacePort:
    """A port exposed by a workspace container.

    Returned by ``workspace_ports``. Distinct from ``PortBinding``
    which is host-level, while this includes the Compose service name.
    """

    service: str
    container_name: str
    host_port: int
    container_port: int
    protocol: str
    host_ip: str


# ── System Detail Models ──────────────────────────────────────────────────────


@dataclass(slots=True)
class CPUUsage:
    """CPU usage snapshot from the remote host.

    Returned by ``system_cpu_usage``. Per-core breakdown is a
    v0.2 feature; v0.1 returns a single-element list with the
    global average.
    """

    cpu_percent: float
    per_core: list[float]
    load_avg_1m: float
    load_avg_5m: float
    load_avg_15m: float
    uptime_seconds: int


@dataclass(slots=True)
class MemoryUsage:
    """Memory usage snapshot from the remote host.

    Returned by ``system_memory_usage``.
    """

    total_mb: float
    used_mb: float
    available_mb: float
    percent: float
    swap_total_mb: float
    swap_used_mb: float
    swap_percent: float


# ── Log Result Models ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class DockerLogResult:
    """Result of fetching Docker container logs.

    Returned by ``docker_container_logs`` and ``logs_docker``.
    """

    container: str
    log_count: int
    logs: str
    truncated: bool = False


@dataclass(slots=True)
class SystemdLogResult:
    """Result of fetching systemd journal logs.

    Returned by ``logs_systemd``.
    """

    unit: str | None
    log_count: int
    logs: str
    truncated: bool = False


@dataclass(slots=True)
class FileLogResult:
    """Result of reading a log file from the remote host.

    Returned by ``logs_file``. Includes file metadata and
    truncated content.
    """

    path: str
    size_bytes: int
    last_modified: str
    lines_read: int
    content: str
    truncated: bool = False


# ── Truncation ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TruncationMeta:
    """Metadata about output truncation applied to a tool response."""

    truncated: bool = False
    total_count: int | None = None
    returned_count: int | None = None
    bytes_limit: int = 0
