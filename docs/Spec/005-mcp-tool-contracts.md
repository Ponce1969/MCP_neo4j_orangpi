# 005 - MCP Tool Contracts

## Overview

This document defines every MCP tool the server exposes: names, input schemas, output schemas, error contracts, and safety classifications.

All tools follow the audit-first, read-only-first philosophy established in specs 001 through 004.

---

# Scope Phasing

## Phase 1 — audit_mcp (v0.1)

Read-only inspection tools. No mutative operations. This is the only phase in initial scope.

## Phase 2 — admin_mcp (v0.2, not yet implemented)

Human-approved mutative operations. Requires a separate MCP namespace or server. Defined here for forward-compatibility awareness but explicitly OUT OF SCOPE for v0.1.

---

# Tool Categories

All v0.1 tools belong to one of five audit categories:

| Category    | Prefix      | Access    | Scope           | Description                        |
| ----------- | ----------- | --------- | --------------- | ---------------------------------- |
| Docker      | `docker_`  | read-only | host-level      | Container and image audit          |
| Network     | `network_` | read-only | host-level      | Port and connectivity audit        |
| System      | `system_`  | read-only | host-level      | Hardware and service audit         |
| Logs        | `logs_`    | read-only | host-level      | Remote log and journal audit       |
| Workspace   | `workspace_` | read-only | workspace-scoped | Project-scoped inspection        |

## Host-Level vs Workspace-Scoped

This distinction is critical and enforced by spec 008 (Workspace Governance).

**Host-level tools** observe the entire OrangePi. They are legitimate for infrastructure observability, port conflict analysis, and system diagnostics. They do NOT violate workspace isolation because they are read-only and do not expose project internals beyond what is visible from the host's perspective.

- `docker_*` — list and inspect all containers on the host
- `network_*` — scan ports, inspect bindings, check Tailscale
- `system_*` — CPU, RAM, disk, temperatures, services
- `logs_*` — read host-level logs (systemd journal, log files)

**Workspace-scoped tools** operate within a registered project workspace. They resolve paths internally from a workspace identifier — the agent never provides raw filesystem paths. They are the ONLY way to interact with Docker Compose projects, project-scoped logs, and project directories.

- `workspace_*` — inspect, compose, logs, ports within a workspace context

A host-level `docker_list_containers` shows all containers across all projects. A `workspace_docker_ps` shows only containers belonging to a specific workspace's Compose project. Both are valuable; they answer different questions.

---

# Tool Metadata Conventions

Every MCP tool declaration includes classification metadata. This metadata is consumed by the MCP handler layer and by agents to understand tool capabilities without inspecting implementation.

## Metadata Fields

Each tool carries these annotations:

| Field                | Type     | Values                                   | Description                                      |
| -------------------- | -------- | ---------------------------------------- | ------------------------------------------------ |
| `readonly`           | bool     | `true` / `false`                         | Whether the tool performs only read operations   |
| `bounded_output`    | bool     | `true` / `false`                         | Whether output is subject to truncation limits  |
| `approval_required`  | bool     | `true` / `false`                         | Whether human approval is mandatory before call  |
| `category`           | str      | `docker` / `network` / `system` / `logs` / `workspace` | Tool category for grouping |
| `max_impact`         | str      | `none` / `low` / `medium` / `high`       | Worst-case impact if the tool misbehaves         |

## Phase 1 Tool Classifications

All v0.1 tools are `readonly: true` and `approval_required: false`:

| Tool                        | readonly | bounded_output | approval_required | category | max_impact |
| --------------------------- | -------- | -------------- | ----------------- | -------- | ---------- |
| `docker_list_containers`    | true     | false          | false             | docker   | none       |
| `docker_inspect_container`  | true     | false          | false             | docker   | none       |
| `docker_container_logs`     | true     | true           | false             | docker   | low        |
| `docker_container_stats`    | true     | false          | false             | docker   | none       |
| `docker_inspect_ports`      | true     | false          | false             | docker   | none       |
| `network_scan_ports`        | true     | true           | false             | network  | low        |
| `network_suggest_port`      | true     | false          | false             | network  | none       |
| `network_inspect_bindings`  | true     | true           | false             | network  | none       |
| `network_tailscale_status`   | true     | false          | false             | network  | none       |
| `system_cpu_usage`           | true     | false          | false             | system   | none       |
| `system_memory_usage`        | true     | false          | false             | system   | none       |
| `system_disk_usage`          | true     | false          | false             | system   | none       |
| `system_temperatures`        | true     | false          | false             | system   | none       |
| `system_service_status`      | true     | false          | false             | system   | none       |
| `logs_docker`                | true     | true           | false             | logs     | low        |
| `logs_systemd`               | true     | true           | false             | logs     | low        |
| `logs_file`                  | true     | true           | false             | logs     | medium     |
| `workspace_list`             | true     | false          | false             | workspace | none     |
| `workspace_inspect`          | true     | false          | false             | workspace | none     |
| `workspace_docker_ps`        | true     | true           | false             | workspace | none     |
| `workspace_logs`             | true     | true           | false             | workspace | low      |
| `workspace_ports`            | true     | true           | false             | workspace | none     |

`max_impact` rationale:
- **none**: Zero side effects. Pure read of system state.
- **low**: Reads potentially large output (logs, port scans). Minimal resource consumption on the remote host.
- **medium**: Reads from arbitrary paths within allowlist. Has file IO implications on the remote host.

## Phase 2 Tool Classifications (Reference Only)

All Phase 2 (admin) tools will be `readonly: false` and `approval_required: true`:

| Tool                        | readonly | bounded_output | approval_required | category | max_impact |
| --------------------------- | -------- | -------------- | ----------------- | -------- | ---------- |
| `docker_restart_container`  | false    | false          | true              | docker   | medium     |
| `docker_stop_container`     | false    | false          | true              | docker   | medium     |
| `docker_start_container`   | false    | false          | true              | docker   | low        |
| `docker_remove_container`   | false    | false          | true              | docker   | high       |
| `service_restart`           | false    | false          | true              | system   | medium     |
| `service_start`             | false    | false          | true              | system   | low        |
| `service_stop`              | false    | false          | true              | system   | medium     |
| `deploy_compose`             | false    | true           | true              | docker   | high       |
| `file_write`                | false    | false          | true              | logs     | high       |
| `port_assign`               | false    | false          | true              | network  | medium     |

---

# Common Types

All tools share these type conventions:

```python
# Duration for time-based queries
type Duration = Literal["5m", "15m", "30m", "1h", "6h", "24h"]

# Line limit for log-like outputs
type LineLimit = int  # 1-500, default 100

# Standard error shape
class ToolError:
    code: str        # Namespaced code, e.g. "CONN_FAILED", "DOCKER_NOT_FOUND"
    message: str     # Human-readable explanation
    detail: dict | None  # Optional structured context
    retryable: bool  # Whether the client should retry

# Standard result shape
class ToolResult:
    data: dict | None       # Present on success
    error: ToolError | None # Present on failure
    truncated: bool          # True if output exceeded payload limit
```

---

# Global Output Truncation Policy

All MCP tool responses are subject to a repository-wide output bounding policy. No tool may return an unbounded payload.

## Limits

| Metric                          | Value    | Configurable via                 |
| ------------------------------- | -------- | -------------------------------- |
| Maximum JSON response payload   | 1 MB     | `ORANPI_MAX_PAYLOAD_KB` (default: 1024) |
| Maximum raw log/text output     | 50 KB    | `ORANPI_LOG_OUTPUT_LIMIT_KB` (default: 50) |
| Maximum list length (containers, ports, services) | 500 items | Hardcoded, not configurable |
| Maximum single field value       | 100 KB   | Hardcoded, not configurable      |

## Truncation Behavior

When a response exceeds its category limit:

1. **Log/text output** (`docker_container_logs`, `logs_systemd`, `logs_file`): truncated from the top (oldest lines dropped). The `truncated` field is set to `true`.
2. **List output** (`docker_list_containers`, `network_scan_ports`, `system_service_status`): truncated to the most recent N items. The response includes `total_count` (actual count) and `returned_count` (items in response). If `total_count > returned_count`, `truncated` is `true`.
3. **JSON payload** (any tool): if the serialized JSON exceeds `ORANPI_MAX_PAYLOAD_KB`, the tool returns a `VALID_PAYLOAD_EXCEEDED` error instead of partial data. The agent should narrow its query (e.g., smaller `tail`, narrower port range).

## Truncation Metadata

Every tool response includes these fields:

```python
class TruncationMeta:
    truncated: bool           # True if output was bounded
    total_count: int | None   # Total items available (for lists)
    returned_count: int | None # Items actually returned (for lists)
    bytes_limit: int           # The byte limit that was applied
```

This metadata allows agents to decide whether to paginate, narrow, or accept partial data.

## Implementation Requirement

The application layer MUST check payload size BEFORE returning to the MCP handler. Truncation happens at the application layer, not the transport layer. The MCP handler never sends a response larger than the configured limit.

---

# Docker Audit Tools

## docker_list_containers

List Docker containers on the remote host.

```
Input:
  all: bool = False        # Include stopped containers
  filter: str | None       # Name substring filter

Output:
  containers: list[ContainerSummary]
  total_count: int           # Total containers available (may differ from len if truncated)
  returned_count: int         # Number of containers in this response

ContainerSummary:
  id: str                  # Short container ID (12 chars)
  name: str                # Container name
  image: str               # Image name
  status: str              # "running" | "exited" | "paused" | ...
  created: str             # ISO 8601 timestamp
  ports: list[PortBinding] # Port mappings

PortBinding:
  host_port: int
  container_port: int
  protocol: str            # "tcp" | "udp"
  host_ip: str             # Usually "0.0.0.0" or specific binding
```

Errors:
- `CONN_FAILED` — SSH unreachable
- `DOCKER_UNAVAILABLE` — Docker daemon not responding

---

## docker_inspect_container

Detailed inspection of a single container.

```
Input:
  container: str           # Container name or ID (required)

Output:
  id: str                  # Full container ID
  name: str
  image: str
  status: str
  created: str
  ports: list[PortBinding]
  networks: list[str]      # Network names
  labels: dict[str, str]   # Container labels
  env: list[str]           # Environment variable NAMES only (never values)
  mounts: list[MountInfo]
  health: HealthStatus | None
  restart_policy: str

MountInfo:
  source: str
  destination: str
  mode: str                # "rw" | "ro"
  type: str                # "bind" | "volume"

HealthStatus:
  status: str              # "healthy" | "unhealthy" | "starting"
  failing_streak: int
  last_output: str         # Last health check output (truncated to 500 chars)
```

Errors:
- `DOCKER_NOT_FOUND` — No container matches
- `CONN_FAILED`

Security note: `env` exposes variable NAMES only, never values. Values may contain secrets.

---

## docker_container_logs

Fetch recent logs from a container.

This tool exists specifically so agents can diagnose errors without the human needing to copy-paste terminal output manually.

```
Input:
  container: str           # Container name or ID (required)
  tail: LineLimit = 100    # Number of lines from the end
  since: str | None        # ISO 8601 timestamp; only logs after this time
  until: str | None        # ISO 8601 timestamp; only logs before this time

Output:
  container: str
  log_count: int
  logs: str                # Raw log output (stdout + stderr interleaved)
  truncated: bool          # True if output exceeded 50KB and was cut
```

Errors:
- `DOCKER_NOT_FOUND`
- `CONN_FAILED`
- `CONN_TIMEOUT`

Safety note: This is read-only. It does NOT stream or follow logs. It fetches a bounded snapshot.

Output limit: Subject to the global truncation policy (see Global Output Truncation Policy). Default 50KB. If logs exceed this, the output is truncated from the top (oldest lines dropped) with `truncated: true`.

---

## docker_container_stats

Resource usage statistics for a container.

```
Input:
  container: str           # Container name or ID (required)

Output:
  container: str
  cpu_percent: float       # CPU usage percentage
  memory_usage_mb: float    # Memory used in MB
  memory_limit_mb: float    # Memory limit in MB
  memory_percent: float     # Memory usage percentage
  network_io: NetworkIO
  block_io: BlockIO
  pids: int

NetworkIO:
  bytes_in: int
  bytes_out: int

BlockIO:
  bytes_read: int
  bytes_written: int
```

Errors:
- `DOCKER_NOT_FOUND`
- `DOCKER_NOT_RUNNING` — Stats only available for running containers
- `CONN_FAILED`

Implementation note: Uses `docker stats --no-stream --format json` for a single-point snapshot. Does NOT stream.

---

## docker_inspect_ports

Focused port inspection across all containers.

```
Input:
  port: int | None          # Specific port to check
  protocol: str = "tcp"     # "tcp" | "udp"

Output:
  ports: list[PortOccupant]

PortOccupant:
  host_port: int
  container_port: int
  protocol: str
  container_name: str
  container_id: str
  host_ip: str
  process: str | None       # Host-side process holding the port, if detectable
```

Errors:
- `CONN_FAILED`

Safety note: If a requested port is occupied, this tool IDENTIFIES the occupant. It does NOT suggest terminating it. Port suggestion is a separate tool (`network_suggest_port`).

---

# Network Audit Tools

## network_scan_ports

Scan which ports are in use on the remote host.

**This tool is bounds-constrained to prevent expensive full-range scans on ARM hardware.**

```
Input:
  range_start: int = 1       # Port range start (must be >= 1)
  range_end: int = 1024      # Port range end (defaults to 1024, NOT 65535)
  protocol: str = "tcp"      # "tcp" | "udp"

Output:
  host: str
  occupied_ports: list[OccupiedPort]
  scan_duration_ms: int
  truncated: bool             # True if results exceeded payload limit

OccupiedPort:
  port: int
  protocol: str
  process: str | None        # Process name, if available
  container: str | None      # Docker container name, if applicable
```

Validation rules:
- `range_start` must be >= 1 and <= `range_end`
- `range_end` must be <= 65535
- Maximum scan range: `range_end - range_start` must be <= 10000
- If the range exceeds 10000, return `NET_SCAN_RANGE_INVALID` error
- Scan timeout: hard cap at 30 seconds (`ORANPI_SSH_COMMAND_TIMEOUT` applies)
- If scan exceeds timeout, return `NET_SCAN_TIMEOUT` error

Default range is 1-1024, NOT 1-65535. Full-range scans on ARM hardware are prohibitively expensive and blocked by the maximum range constraint.

Errors:
- `CONN_FAILED`
- `NET_SCAN_TIMEOUT` — Scan exceeded command timeout
- `NET_SCAN_RANGE_INVALID` — Range exceeds 10000 ports or start > end
- `VALID_PARAM_OUT_OF_RANGE` — Port values outside 1-65535

---

## network_suggest_port

Suggest available ports near a desired port.

```
Input:
  preferred_port: int        # The port you'd prefer to use
  range: int = 100           # How far from preferred_port to search
  count: int = 5             # How many suggestions to return

Output:
  preferred_port: int
  preferred_available: bool   # Is the preferred port free?
  suggestions: list[PortSuggestion]

PortSuggestion:
  port: int
  available: bool
  occupant: str | None        # Description of what's using it, if occupied
```

Errors:
- `CONN_FAILED`

Safety note: This tool DOES NOT allocate or reserve ports. It only suggests availability. The human must approve any actual port usage.

---

## network_inspect_bindings

Show all active network bindings (TCP + UDP listeners).

```
Input: None

Output:
  bindings: list[NetworkBinding]

NetworkBinding:
  local_address: str          # e.g. "0.0.0.0:5432" or "127.0.0.1:3000"
  port: int
  protocol: str               # "tcp" | "udp"
  process: str | None
  pid: int | None
  container: str | None        # Mapped to Docker container if applicable
```

Errors:
- `CONN_FAILED`

---

## network_tailscale_status

Report Tailscale VPN status on the remote host.

```
Input: None

Output:
  online: bool
  tailscale_ip: str | None
  public_ip: str | None
  hostname: str
  dns_name: str | None
  connections: list[TailscalePeer]

TailscalePeer:
  name: str
  tailscale_ip: str
  online: bool
  last_seen: str | None        # ISO 8601
```

Errors:
- `NET_TAILSCALE_NOT_INSTALLED`

---

# System Audit Tools

## system_cpu_usage

CPU utilization snapshot.

```
Input:
  duration: Duration = "5m"   # Averaging window (best-effort, may vary)

Output:
  cpu_percent: float           # Overall CPU usage
  per_core: list[float]        # Per-core usage
  load_avg_1m: float
  load_avg_5m: float
  load_avg_15m: float
  uptime_seconds: int
```

Errors:
- `CONN_FAILED`

---

## system_memory_usage

Memory usage snapshot.

```
Input: None

Output:
  total_mb: float
  used_mb: float
  available_mb: float
  percent: float
  swap_total_mb: float
  swap_used_mb: float
  swap_percent: float
```

Errors:
- `CONN_FAILED`

---

## system_disk_usage

Disk usage for mounted filesystems.

```
Input:
  path: str | None = None     # Specific mount path, or all

Output:
  disks: list[DiskInfo]

DiskInfo:
  mount_point: str
  device: str
  total_gb: float
  used_gb: float
  available_gb: float
  percent: float
  filesystem_type: str
```

Errors:
- `CONN_FAILED`

---

## system_temperatures

Hardware temperature readings (ARM SoC specific).

```
Input: None

Output:
  temperatures: list[SensorReading]
  throttled: bool               # Whether thermal throttling is active

SensorReading:
  name: str                     # e.g. "cpu", "gpu"
  temp_c: float
  critical_c: float | None      # Threshold if available
```

Errors:
- `CONN_FAILED`
- `SYS_SENSORS_UNAVAILABLE` — No thermal sensors found

---

## system_service_status

Status of systemd services.

```
Input:
  service: str | None = None    # Specific service name, or list all

Output:
  services: list[ServiceInfo]

ServiceInfo:
  name: str
  active: str                    # "active" | "inactive" | "failed" | ...
  sub_state: str                 # e.g. "running", "exited"
  enabled: bool                  # Starts on boot?
  description: str
```

Errors:
- `CONN_FAILED`
- `SYS_SERVICE_NOT_FOUND` — Specific service not found

Safety note: Only reports status. Does NOT start, stop, or restart services.

---

# Log Audit Tools

This category addresses the core need: agents can read remote logs without the human needing to manually copy-paste terminal output from the OrangePi.

These are **host-level** log tools. For workspace-scoped logs, use `workspace_logs`.

## logs_docker

Fetch Docker container logs. Alias for `docker_container_logs` with a convenient name for the logs-first mental model.

```
Input:
  container: str               # Container name or ID (required)
  tail: LineLimit = 100
  since: str | None

Output:
  Same as docker_container_logs
```

---

## logs_systemd

Read systemd journal logs from the remote host.

```
Input:
  unit: str | None = None      # Service unit name (e.g. "nginx", "docker")
  priority: str = "info"        # "emerg" | "alert" | "crit" | "err" | "warning" | "notice" | "info" | "debug"
  tail: LineLimit = 100
  since: str | None             # ISO 8601 or systemd time format
  until: str | None

Output:
  unit: str | None
  log_count: int
  logs: str                     # Raw journal output
  truncated: bool
```

Errors:
- `CONN_FAILED`
- `LOG_UNIT_NOT_FOUND` — Specific service unit not found

Output limit: Maximum 50KB per response.

---

## logs_file

Read the tail of a specific log file on the remote host.

**This tool has strict safety constraints to prevent heavy disk IO and arbitrary file reads.**

```
Input:
  path: str                     # Absolute file path (required)
  tail: LineLimit = 100
  since: str | None             # ISO 8601 timestamp

Output:
  path: str
  size_bytes: int
  last_modified: str            # ISO 8601
  lines_read: int
  content: str
  truncated: bool
```

Errors:
- `CONN_FAILED`
- `LOG_FILE_NOT_FOUND`
- `LOG_FILE_NOT_READABLE`
- `LOG_PATH_FORBIDDEN` — Path is outside allowed directories
- `LOG_FILE_OVERSIZED` — File exceeds maximum readable size

### File Access Rules

1. **Allowlist enforcement**: File access is restricted to directories defined by `ORANPI_ALLOWED_LOG_DIRS`. Default allowlist:

```
/var/log/
/home/
/opt/
/tmp/*.log
```

2. **Symlink resolution**: All symlinks are resolved before allowlist checking. If the resolved path is outside allowed directories, the request is rejected with `LOG_PATH_FORBIDDEN`. This prevents symlink traversal attacks.

3. **Maximum readable file size**: Files larger than `ORANPI_MAX_LOG_FILE_MB` (default: 50 MB) are rejected with `LOG_FILE_OVERSIZED`. The tool does NOT attempt to read oversized files; it checks the file size via `stat` before reading.

4. **Read-only guarantee**: The tool uses `tail` to read from the end of the file. It never writes, creates, or modifies files.

5. **Path traversal prevention**: Paths containing `..` are rejected. Paths are normalized and resolved before allowlist checking.

6. **Binary file rejection**: If the file appears to be binary (first 512 bytes contain null bytes), the tool returns `LOG_FILE_NOT_READABLE` with an appropriate message.

### Implementation Requirement

The remote execution sequence MUST be:

```
1. stat {path} → get file size, reject if > ORANPI_MAX_LOG_FILE_MB
2. Resolve symlinks, check against allowlist
3. tail -n {tail} {path} → read only the requested lines
```

Step 1 prevents reading large files. Step 2 prevents path traversal. Step 3 ensures bounded IO.

---

# Workspace Audit Tools

This category provides project-scoped inspection within registered workspaces (spec 008). The agent references workspaces by **logical identifier only** — the MCP resolves real paths internally.

## Workspace Governance Principles

These tools enforce:

1. **Registered workspaces only**: The agent cannot access paths outside registered workspaces.
2. **Logical identifiers**: The agent passes `workspace: "guardian"`, never `/home/gonzalo/Gonzalo_codigo/guardian`.
3. **No arbitrary paths**: No tool accepts a raw filesystem path from the agent.
4. **Read-only**: All v0.1 workspace tools are read-only.
5. **Compose-resolved cwd**: Docker Compose commands execute from the workspace directory, resolved internally.

---

## workspace_list

List all registered workspaces.

```
Input: None

Output:
  workspaces: list[WorkspaceInfo]

WorkspaceInfo:
  id: str                     # Logical identifier, e.g. "guardian"
  path: str                   # Resolved absolute path on the OrangePi
  status: str                 # "active" | "disabled" | "unreachable"
  has_compose: bool           # Whether docker-compose.yml or compose.yml exists
  has_git: bool                # Whether .git directory exists
  project_type: str | None    # Detected project type: "python", "node", "go", etc.
```

Errors:
- `CONN_FAILED`

Notes:
- `disabled` status means the workspace directory was not found on the OrangePi during the last validation
- `unreachable` means the SSH connection failed during validation
- This tool is the entry point for workspace-scoped operations. The agent should call this first, then select a workspace.

---

## workspace_inspect

Get detailed metadata about a single workspace.

```
Input:
  workspace: str               # Logical workspace identifier (required)

Output:
  id: str
  path: str
  status: str                  # "active" | "disabled" | "unreachable"
  has_compose: bool
  has_git: bool
  project_type: str | None
  detected_services: list[str] # e.g. ["postgresql", "redis", "nginx"]
  docker_compose_project: str | None  # Docker Compose project name, if compose exists
  last_validated: str          # ISO 8601 timestamp of last validation

  ComposeInfo (if has_compose):
    services: list[str]        # Service names from compose config
    networks: list[str]        # Network names
    volumes: list[str]         # Volume names
```

Errors:
- `CONN_FAILED`
- `WS_NOT_FOUND` — Workspace identifier is not registered
- `WS_DISABLED` — Workspace directory does not exist on the remote host
- `WS_UNREACHABLE` — Cannot verify workspace state due to SSH failure

---

## workspace_docker_ps

List Docker containers belonging to a specific workspace's Compose project.

This tool executes `docker compose ps` from the workspace directory, showing only containers that belong to that project.

```
Input:
  workspace: str               # Logical workspace identifier (required)

Output:
  workspace: str
  path: str                   # Resolved workspace path (for reference, not agent use)
  containers: list[ComposeContainer]

ComposeContainer:
  name: str                   # Container name
  service: str                # Compose service name
  state: str                  # "running" | "exited" | "paused" | ...
  health: str | None          # Health status if available
  ports: list[PortBinding]
  created: str                # ISO 8601
```

Errors:
- `CONN_FAILED`
- `WS_NOT_FOUND`
- `WS_DISABLED`
- `WS_NO_COMPOSE` — Workspace has no docker-compose.yml

Implementation note: This tool resolves the workspace path from the registry, then executes `docker compose ps` from that directory on the remote host. The agent never provides the path.

---

## workspace_logs

Fetch logs from containers belonging to a specific workspace's Compose project.

This tool scopes Docker logs to a workspace, preventing cross-project log leakage.

```
Input:
  workspace: str               # Logical workspace identifier (required)
  service: str | None          # Specific compose service, or all services
  tail: LineLimit = 100
  since: str | None            # ISO 8601 timestamp

Output:
  workspace: str
  service: str | None
  log_count: int
  logs: str                    # Raw log output
  truncated: bool
```

Errors:
- `CONN_FAILED`
- `WS_NOT_FOUND`
- `WS_DISABLED`
- `WS_NO_COMPOSE`
- `DOCKER_NOT_FOUND` — Specified service container not found

Implementation note: Executes `docker compose logs` from the workspace directory. Output is subject to the global truncation policy (default 50KB).

---

## workspace_ports

Show port bindings for containers in a specific workspace's Compose project.

```
Input:
  workspace: str               # Logical workspace identifier (required)

Output:
  workspace: str
  path: str
  ports: list[WorkspacePort]

WorkspacePort:
  service: str                # Compose service name
  container_name: str
  host_port: int
  container_port: int
  protocol: str
  host_ip: str
```

Errors:
- `CONN_FAILED`
- `WS_NOT_FOUND`
- `WS_DISABLED`
- `WS_NO_COMPOSE`

 ---

All tools follow a consistent error contract:

```python
class ToolResult:
    data: dict | None       # Present on success
    error: ToolError | None # Present on failure
```

## Error Code Namespace

Every error code follows the pattern `CATEGORY_SPECIFIC_ERROR` with consistent prefixes. There are no bare names; all codes are prefixed (e.g., `CONN_FAILED`, not `CONNECTION_FAILED`).

| Prefix      | Category        | Description                        |
| ----------- | --------------- | ---------------------------------- |
| `CONN_`     | Connection      | SSH, network, transport failures   |
| `DOCKER_`   | Docker          | Container, image, daemon errors    |
| `NET_`      | Network         | Port scan, binding, VPN errors     |
| `SYS_`      | System          | CPU, memory, disk, sensor errors   |
| `LOG_`      | Logs            | Log file, journal access errors    |
| `VALID_`    | Validation      | Input parameter validation errors  |
| `AUTH_`     | Authorization   | Permission and auth errors         |

## Complete Error Code Registry

### Connection Errors (`CONN_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `CONN_FAILED`         | SSH connection unreachable or dropped                 |
| `CONN_TIMEOUT`        | SSH connection or command timed out                   |
| `CONN_RECONNECTING`   | Connection lost, background reconnection in progress  |

### Docker Errors (`DOCKER_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `DOCKER_UNAVAILABLE`  | Docker daemon not responding on the remote host       |
| `DOCKER_NOT_FOUND`    | Requested container or resource does not exist        |
| `DOCKER_NOT_RUNNING`  | Container exists but is not running (e.g., stats)      |

### Network Errors (`NET_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `NET_SCAN_TIMEOUT`    | Port scan exceeded allowed time                       |
| `NET_SCAN_RANGE_INVALID` | Port range is invalid or exceeds maximum           |
| `NET_TAILSCALE_NOT_INSTALLED` | Tailscale is not installed on the host       |

### System Errors (`SYS_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `SYS_SERVICE_NOT_FOUND` | Requested systemd service does not exist           |
| `SYS_SENSORS_UNAVAILABLE` | Hardware sensors not found or not supported      |

### Log Errors (`LOG_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `LOG_PATH_FORBIDDEN`  | File path is outside allowed directories              |
| `LOG_FILE_NOT_FOUND`  | Requested log file does not exist                     |
| `LOG_FILE_NOT_READABLE` | File exists but cannot be read (permissions)         |
| `LOG_FILE_OVERSIZED`  | File exceeds maximum readable size                    |
| `LOG_UNIT_NOT_FOUND`  | Requested systemd unit does not exist                 |

### Validation Errors (`VALID_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `VALID_PARAM_INVALID` | Generic parameter validation failure                  |
| `VALID_PARAM_OUT_OF_RANGE` | Numeric parameter outside allowed bounds        |
| `VALID_PARAM_REGEX_FAIL` | Parameter does not match required regex pattern    |
| `VALID_PARAM_REQUIRED`  | Required parameter is missing or empty               |
| `VALID_PAYLOAD_EXCEEDED` | Response payload exceeds maximum allowed size       |

### Authorization Errors (`AUTH_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `AUTH_FORBIDDEN`      | Operation not allowed by security policy              |
| `AUTH_HOST_KEY_REJECTED` | Host key verification failed                       |

### Workspace Errors (`WS_`)

| Code                  | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `WS_NOT_FOUND`       | Workspace identifier is not registered                |
| `WS_DISABLED`        | Workspace directory does not exist on the remote host |
| `WS_UNREACHABLE`      | Cannot verify workspace due to SSH failure             |
| `WS_NO_COMPOSE`       | Workspace has no docker-compose.yml file               |
| `WS_PATH_ESCAPE`      | Resolved workspace path escapes root directory         |

## Error Response Shape

Every error includes structured context:

```python
class ToolError:
    code: str              # From the registry above (e.g., "CONN_FAILED")
    message: str           # Human-readable explanation
    detail: dict | None    # Optional structured context
    retryable: bool         # Whether the client should retry (e.g., CONN_RECONNECTING=True)
```

The `retryable` field helps agents decide whether to retry or report failure.

---

# Forward-Compatibility: Admin Tools (Phase 2, OUT OF SCOPE)

These tools are EXCLUDED from v0.1. They are documented here to prevent naming collisions and ensure the audit-first design accounts for their eventual existence.

## Admin tools not yet implemented

| Tool                        | Description                    | Approval Required |
| --------------------------- | ------------------------------ | ----------------- |
| `docker_restart_container`  | Restart a container            | Yes               |
| `docker_stop_container`     | Stop a running container       | Yes               |
| `docker_start_container`   | Start a stopped container      | Yes               |
| `docker_remove_container`   | Remove a stopped container     | Yes               |
| `service_restart`           | Restart a systemd service      | Yes               |
| `service_start`             | Start a systemd service        | Yes               |
| `service_stop`              | Stop a systemd service         | Yes               |
| `deploy_compose`             | Deploy a docker-compose stack  | Yes               |
| `file_write`                | Write a file to the host       | Yes               |
| `port_assign`               | Assign a service to a port     | Yes               |

Admin tools will use a confirmation token model:

1. Agent calls an admin tool
2. MCP returns a `PENDING_APPROVAL` response with a unique token
3. Human reviews the request and calls `admin_approve` or `admin_reject`
4. Only then does the MCP execute the operation

This model prevents autonomous mutative operations by design.

---

# Risks and Tradeoffs

## Risk: Overly broad log access

`logs_file` reads arbitrary files within allowed directories. If someone symlinks `/var/log/../../../etc/shadow` inside `/var/log/`, it could expose secrets.

**Mitigation**: Resolve symlinks before checking allowlist. Reject any resolved path outside allowed directories. Document this clearly.

## Risk: `docker_container_logs` leaking secrets in env vars

Containers may log secrets.

**Mitigation**: `docker_inspect_container` exposes env NAMES only, never values. Log output is the container's own stdout/stderr — the MCP cannot redact application-level secrets. This is an accepted risk; document that agents should not persist raw log output.

## Risk: Port scan performance on ARM hardware

`network_scan_ports` with a large range is expensive on OrangePi ARM hardware.

**Mitigation**: Default range is 1-1024 (not 1-65535). Maximum range is capped at 10000 ports. Validation rejects oversized ranges with `NET_SCAN_RANGE_INVALID`. Hard timeout via `ORANPI_SSH_COMMAND_TIMEOUT` (default 30s).

## Risk: Log file reads causing heavy disk IO on remote host

`logs_file` reads from the remote filesystem. Oversized files could cause heavy IO and memory pressure on the OrangePi.

**Mitigation**: Files larger than `ORANPI_MAX_LOG_FILE_MB` (default 50 MB) are rejected before reading (size checked via `stat`). The tool reads only the last N lines via `tail`, never the entire file. Binary files are rejected by checking the first 512 bytes.

## Tradeoff: Output truncation policy

The global truncation policy (50KB for logs, 1MB for JSON payloads) balances usefulness against token cost and remote host resource consumption. Very verbose containers may need multiple calls with `since` parameters.

**Accepted tradeoff**: Agents can paginate via `since`/`until` parameters. The limit prevents context explosion and protects the OrangePi from excessive IO.

## Tradeoff: stdout format for logs vs structured JSON

Using raw string output for logs (`logs: str`) is simpler and preserves the original format (timestamps, log levels, multiline stack traces). Structured JSON would lose formatting and complicate implementation.

**Accepted tradeoff**: Raw output with bounding. The agent can reason about the text directly.

## Tradeoff: Separate `logs_docker` alias

Having `logs_docker` as an alias for `docker_container_logs` creates a naming collision risk. However, it provides a clearer mental model for the "I need logs" use case.

**Accepted tradeoff**: Keep the alias. The implementation is shared; only the tool name differs. Document that `logs_docker` and `docker_container_logs` are equivalent.

## Risk: Host-level tools expose cross-project data

`docker_list_containers` shows containers from all projects. `docker_inspect_container` can inspect any container. `network_inspect_bindings` shows all listeners. This is by design — these are host-level observability tools.

**Mitigation**: These tools are read-only. They show what is already visible from the host's perspective. For project-scoped inspection, use `workspace_*` tools which filter to a specific Compose project. The agent should default to `workspace_*` tools for project-specific questions and use host-level tools only for infrastructure-level diagnostics.

**Accepted tradeoff**: Removing host-level tools would blind the agent to port conflicts, resource contention, and system health. The two-level model (host observability + workspace scoping) gives the right visibility at the right level.

## Risk: Docker Compose project name collision

Two workspaces might use the same Docker Compose project name (default: directory name). If both are named `app`, `docker compose ps` run in the wrong directory could show containers from a different project.

**Mitigation**: `workspace_docker_ps` resolves the workspace path from the registry and executes `docker compose ps` from that directory. The compose project name is derived from the directory, so as long as workspace directories have unique names, project names are unique. The `workspace_inspect` tool reports `docker_compose_project` so the agent can verify.

## Risk: Workspace path resolution and escape

If the workspace registry or the remote host has symlinks, a workspace path might resolve outside the root workspace directory.

**Mitigation**: All workspace paths are resolved and validated at startup. Symlinks are followed to their real path, which must be under `ORANPI_ROOT_WORKSPACE_DIR`. The `WS_PATH_ESCAPE` error code covers this case. The agent never provides raw paths; only logical identifiers.

## Tradeoff: Two parallel path systems (host logs vs workspace)

`logs_file` uses `ORANPI_ALLOWED_LOG_DIRS` (host-level path allowlist). `workspace_logs` uses workspace-scoped Compose logs. These are intentionally separate systems.

**Accepted tradeoff**: Host-level logs (`/var/log/syslog`, `/var/log/docker`) are infrastructure diagnostics, not project data. Workspace logs are project-scoped. Confusing them would violate workspace isolation. Keep both systems isolated.