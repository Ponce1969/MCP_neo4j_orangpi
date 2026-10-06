# 006 - Runtime and Connectivity

## Overview

This document defines the MCP server's runtime configuration, SSH connectivity strategy, project structure, logging, and OpenCode integration.

---

# Transport: stdio

The MCP server communicates over **stdio** (standard input/output).

This is the standard transport for local MCP servers consumed by tools like OpenCode. The server reads JSON-RPC from stdin and writes JSON-RPC to stdout. No HTTP server, no SSE, no websockets.

The MCP SDK handles the stdio framing. The server entrypoint simply starts the server lifecycle.

```
OpenCode Agent
      │
      ▼ (stdio JSON-RPC)
Local MCP Server Process
      │
      ▼ (asyncssh)
OrangePi5 Plus
```

Why stdio:
- OpenCode natively supports stdio MCP servers
- No port conflicts on the dev machine
- Simpler security (no network listener)
- One process per agent session, automatically managed

Why NOT HTTP/SSE for v0.1:
- Adds complexity (HTTP server lifecycle, CORS, auth)
- Not needed for single-user local usage
- Can be added later without redesigning tools

---

# SSH Connectivity Strategy

## Library: asyncssh

The project uses `asyncssh` for all SSH connections. No subprocess calls to `ssh`, no `paramiko`, no `fabric`.

Why asyncssh:
- Native asyncio (matches async-first architecture)
- Type-annotated API
- SSH key authentication built-in
- Full SFTP support (future file logging)
- Actively maintained

## Connection Model: Persistent Connection per Session

The MCP server maintains a single persistent SSH connection for its lifetime.

```
MCP Server Start
      │
      ▼
Connect to OrangePi via asyncssh
      │
      ▼
Keep connection alive (keepalive)
      │
      ▼
Execute all commands over this connection
      │
      ▼
MCP Server Stop → Close connection
```

Why persistent:
- Avoids connection overhead per tool call (SSH handshake is ~200ms)
- Reuses authenticated channel
- Simpler reconnection logic (one connection to manage)
- The MCP server is long-lived per agent session

Why NOT per-command:
- Each SSH handshake adds latency
- Key auth handshake repeated unnecessarily
- Connection pool adds complexity for no benefit in single-target scenarios

## Connection Configuration

```python
class SSHConfig:
    host: str              # ORANPI_SSH_HOST
    port: int = 22         # ORANPI_SSH_PORT
    username: str           # ORANPI_SSH_USER
    key_path: Path         # ORANPI_SSH_KEY_PATH
    known_hosts: Path | None  # ORANPI_SSH_KNOWN_HOSTS
    security_mode: Literal["production", "development"]  # ORANPI_SSH_SECURITY_MODE
    host_key_policy: Literal["strict", "accept_new", "ssh_config"]  # ORANPI_SSH_HOST_KEY_POLICY
    connect_timeout: int = 10  # seconds
    keepalive_interval: int = 30  # seconds
    command_timeout: int = 30     # seconds per remote command
```

## Workspace Configuration

```python
class WorkspaceConfig:
    id: str                 # Logical identifier, e.g. "guardian"
    path: str              # Absolute path on the OrangePi, e.g. "/home/cerra/codigo/guardian"

class AppConfig:
    ssh: SSHConfig
    root_workspace_dir: str    # ORANPI_ROOT_WORKSPACE_DIR
    workspaces: list[WorkspaceConfig]  # Loaded from workspaces.yaml
    allowed_log_dirs: list[str]    # ORANPI_ALLOWED_LOG_DIRS
    max_payload_kb: int            # ORANPI_MAX_PAYLOAD_KB
    log_output_limit_kb: int      # ORANPI_LOG_OUTPUT_LIMIT_KB
    max_log_file_mb: int          # ORANPI_MAX_LOG_FILE_MB
```

The `AppConfig` is the single source of truth for all runtime configuration. It replaces the previous `SSHConfig` as the top-level config object. `SSHConfig` becomes a nested field within `AppConfig`.

### Workspace Registry Validation

At startup, the MCP validates all registered workspaces:

1. Load `workspaces.yaml` from `ORANPI_WORKSPACE_CONFIG` path
2. For each workspace, verify that its `path` starts with `ORANPI_ROOT_WORKSPACE_DIR`
3. Resolve symlinks and reject any path that escapes the root directory → mark as `disabled` with `WS_PATH_ESCAPE`
4. For each valid workspace, SSH to the OrangePi and check if the directory exists
5. Mark unreachable directories as `unreachable` (SSH failure) or `disabled` (directory not found)
6. Active workspaces are marked `active` and available for tool calls

Invalid workspaces do NOT prevent the MCP from starting. They are logged and marked `disabled`. Only tools that reference a `disabled` workspace return `WS_DISABLED` errors.

### Reconnection Policy

If the SSH connection drops during the server lifetime, the reconnection policy defined in the "Retry Policy" section applies. In summary:

1. First attempt: reconnect immediately
2. Second attempt: wait 5 seconds, retry
3. Third attempt: wait 15 seconds, retry
4. After 3 failures: return `CONN_RECONNECTING` error for all pending tool calls
5. Background: attempt reconnection every 30 seconds
6. If reconnection succeeds: resume normal operation

The MCP server does NOT crash on SSH failure. It reports errors to the agent and keeps trying.

### Connection Health Check

Every 60 seconds, execute a lightweight command (`echo OK`) on the remote host. If this fails, mark the connection unhealthy and trigger reconnection logic.

### Host Key Verification

Host key verification is MANDATORY in production. The server MUST verify the remote host's key against `known_hosts`.

Three configuration modes:
1. **Strict** (default): Reject connection if host key is not in `known_hosts`
2. **Accept new**: Accept and store new host keys (useful for first connection)
3. **Use ssh-config**: Read `~/.ssh/config` for host-specific settings

Mode is controlled by `ORANPI_SSH_HOST_KEY_POLICY`:
- `strict` (default)
- `accept_new`
- `ssh_config`

### SSH Security Modes

The MCP server operates in one of two security modes, controlled by `ORANPI_SSH_SECURITY_MODE`:

#### Production Mode (default: `production`)

- Host key verification is **strict**: `ORANPI_SSH_HOST_KEY_POLICY` defaults to `strict`
- SSH key file must have restrictive permissions (0600 on Linux/macOS, checked on startup)
- No environment variable secrets are logged
- `ORANPI_LOG_LEVEL` minimum is `INFO`
- Connection failures are logged but never reveal key contents or host details beyond hostname

#### Development Mode (`development`)

- Host key verification defaults to `accept_new` if `ORANPI_SSH_HOST_KEY_POLICY` is not explicitly set
- SSH key file permissions are still checked but a warning is logged instead of refusing startup
- Debug-level logging is available (`ORANPI_LOG_LEVEL=DEBUG`)
- Connection failures include more diagnostic detail (expected key, received key fingerprint)
- A startup banner is logged: `⚠️  RUNNING IN DEVELOPMENT MODE — host key verification is relaxed`

#### Mode Comparison

| Behavior                        | `production`               | `development`                        |
| ------------------------------- | -------------------------- | ------------------------------------- |
| Host key policy default         | `strict`                   | `accept_new` (if not explicitly set)  |
| Key file permission check       | Refuse startup             | Warn and continue                    |
| Log level minimum               | `INFO`                     | `DEBUG` allowed                       |
| Connection failure diagnostics  | Hostname only              | Full diagnostics                      |
| Startup banner                  | None                       | Development mode warning              |

The security mode is NOT a secret — it is a startup configuration. It cannot be changed at runtime.

**`development` mode MUST NEVER be used in any environment that touches production infrastructure.**

---

# Environment Variables

All configuration is via environment variables. No hardcoded secrets, no config files with credentials.

## Required

| Variable                  | Description                          | Example                      |
| ------------------------- | ------------------------------------ | ---------------------------- |
| `ORANPI_SSH_HOST`         | OrangePi hostname or IP              | `oranpi.local` or `100.x.x.x` (Tailscale) |
| `ORANPI_SSH_USER`         | SSH username on the OrangePi         | `cerra`                      |
| `ORANPI_SSH_KEY_PATH`     | Path to SSH private key              | `~/.ssh/id_oranpi`           |

## Optional with Defaults

| Variable                      | Default          | Description                              |
| ----------------------------- | ---------------- | ---------------------------------------- |
| `ORANPI_SSH_PORT`             | `22`             | SSH port                                 |
| `ORANPI_SSH_KNOWN_HOSTS`     | `~/.ssh/known_hosts` | Path to known_hosts file             |
| `ORANPI_SSH_HOST_KEY_POLICY` | `strict`         | `strict` / `accept_new` / `ssh_config`  |
| `ORANPI_SSH_SECURITY_MODE`   | `production`    | `production` / `development`             |
| `ORANPI_SSH_CONNECT_TIMEOUT`  | `10`             | Connection timeout in seconds            |
| `ORANPI_SSH_COMMAND_TIMEOUT`  | `30`             | Command execution timeout in seconds     |
| `ORANPI_LOG_LEVEL`            | `INFO`           | `DEBUG` / `INFO` / `WARNING` / `ERROR`   |
| `ORANPI_ALLOWED_LOG_DIRS`     | `/var/log,/home,/opt` | Comma-separated allowed dirs for logs_file |
| `ORANPI_LOG_OUTPUT_LIMIT_KB`  | `50`             | Max log output in KB per response        |
| `ORANPI_MAX_PAYLOAD_KB`       | `1024`           | Max total JSON payload in KB per response |
| `ORANPI_MAX_LOG_FILE_MB`      | `50`             | Max readable log file size in MB         |

## Workspace Configuration

| Variable                      | Default          | Description                              |
| ----------------------------- | ---------------- | ---------------------------------------- |
| `ORANPI_ROOT_WORKSPACE_DIR`  | *(required)*     | Root directory for all workspaces on the OrangePi (e.g. `/home/cerra/codigo`) |
| `ORANPI_WORKSPACE_CONFIG`    | `workspaces.yaml`| Path to workspace configuration file (local, not on OrangePi) |

The workspace configuration file is version-controlled alongside the MCP repository. It defines the mapping between logical workspace identifiers and their paths on the OrangePi.

Example `workspaces.yaml`:

```yaml
workspaces:
  guardian:
    path: /home/cerra/codigo/guardian
  crm:
    path: /home/cerra/codigo/crm
  lab:
    path: /home/cerra/codigo/lab
```

The `path` values are relative to the OrangePi filesystem, NOT the machine running the MCP. The MCP resolves these paths during SSH command execution.

Key constraints:
- All workspace paths MUST be under `ORANPI_ROOT_WORKSPACE_DIR`
- The MCP validates this at startup and marks escaping paths as `disabled`
- The agent NEVER sees or provides raw filesystem paths — only logical identifiers

## .env.example (mandatory per spec 003)

A `.env.example` file MUST be committed at the repository root with all variables, their defaults, and descriptions. No actual secrets in `.env.example`.

The `.gitignore` MUST exclude `.env`.

---

# Project Structure

## Directory Layout

```
mcp-oranpi/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── .python-version
├── workspaces.yaml              # Workspace registry (version-controlled)
├── docs/
│   └── Spec/
│       ├── 001-product-context.md
│       ├── 002-architecture.md
│       ├── 003-engineering-standards.md
│       ├── 004-security-boundaries.md
│       ├── 005-mcp-tool-contracts.md
│       ├── 006-runtime-and-connectivity.md
│       └── 007-testing-strategy.md
├── src/
│   └── mcp_oranpi/
│       ├── __init__.py
│       ├── server.py              # MCP server entry point (stdio)
│       ├── config.py              # Typed settings (pydantic-settings)
│       │
│       ├── domain/
│       │   ├── __init__.py
│       │   ├── models.py          # Domain models (ContainerInfo, PortBinding, etc.)
│       │   ├── errors.py          # Domain error types (ToolError, ConnectionError)
│       │   ├── contracts.py       # Tool input/output type definitions
│       │   └── workspace.py       # Workspace registry models and validation
│       │
│       ├── application/
│       │   ├── __init__.py
│       │   ├── docker_tools.py    # Docker audit tool implementations
│       │   ├── network_tools.py   # Network audit tool implementations
│       │   ├── system_tools.py    # System audit tool implementations
│       │   ├── logs_tools.py      # Log audit tool implementations
│       │   └── workspace_tools.py # Workspace-scoped tool implementations
│       │
│       ├── infrastructure/
│       │   ├── __init__.py
│       │   ├── ssh_client.py      # asyncssh connection management
│       │   ├── command_runner.py  # Safe command execution (ALLOWED_COMMANDS)
│       │   ├── workspace_resolver.py # Workspace path resolution and validation
│       │   └── parsers.py        # Raw command output → domain models
│       │
│       └── presentation/
│           ├── __init__.py
│           └── mcp_handlers.py    # MCP tool → application layer wiring
│
├── tests/
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_parsers.py
│   │   ├── test_models.py
│   │   ├── test_config.py
│   │   ├── test_command_runner.py
│   │   └── test_workspace_resolver.py
│   ├── integration/
│   │   ├── test_docker_tools.py
│   │   ├── test_network_tools.py
│   │   ├── test_system_tools.py
│   │   ├── test_logs_tools.py
│   │   └── test_workspace_tools.py
│   └── fixtures/
│       ├── docker_ps_output.txt
│       ├── docker_stats_output.json
│       └── ...
│
└── scripts/
    └── vulnerability_scanner.py  # Already specified in AGENTS.md
```

## Architecture Conventions

The project follows **hexagonal architecture** (ports and adapters):

```
domain/        → Core models, contracts, errors. No external dependencies.
application/   → Tool implementations using domain models. Calls infrastructure through ports.
infrastructure/→ SSH client, command runner, output parsers. Implements ports.
presentation/  → MCP handler wiring. Maps MCP calls to application layer.
```

Dependency direction: `presentation → application → domain ← infrastructure`

The `domain` layer has ZERO knowledge of:
- MCP SDK
- asyncssh
- SSH commands
- Output formats

The `infrastructure` layer knows about SSH but NOT about MCP.

The `presentation` layer knows about MCP but NOT about SSH.

---

# Asyncio Cancellation Behavior

## Overview

The MCP server is an asyncio application. Asyncio cancellation is part of normal operation — the server lifecycle, tool timeouts, and shutdown all use `asyncio.CancelledError`.

## Mandatory Rules

### Rule 1: Never swallow CancelledError

`asyncio.CancelledError` MUST always be re-raised. It is NEVER acceptable to catch and discard it.

**FORBIDDEN:**

```python
try:
    await some_operation()
except Exception:          # ← catches CancelledError in Python 3.9+
    pass                    # ← NEVER

except asyncio.CancelledError:
    pass                    # ← NEVER
```

**REQUIRED:**

```python
try:
    await some_operation()
except asyncio.CancelledError:
    # Cleanup if needed, then re-raise
    logger.info("operation_cancelled")
    raise
```

### Rule 2: Cleanup before re-raising

If cancellation requires cleanup (closing connections, releasing resources), perform the cleanup THEN re-raise:

```python
async def execute_command(cmd: str) -> str:
    try:
        return await ssh_client.run(cmd, timeout=command_timeout)
    except asyncio.CancelledError:
        await ssh_client.close_channel()
        raise
```

### Rule 3: Use `asyncio.timeout` for command timeouts

Never use `asyncio.wait_for` with bare exception handling. Use `asyncio.timeout` (Python 3.11+) which properly manages cancellation:

```python
async with asyncio.timeout(command_timeout):
    result = await ssh_client.run(cmd)
```

### Rule 4: Graceful shutdown

On `SIGINT` or `SIGTERM`:

1. Cancel all pending tool tasks
2. Wait for in-progress SSH commands to complete or timeout (max 5 seconds)
3. Close SSH connection
4. Log shutdown event
5. Exit 0

Cancelled tool calls return `CONN_TIMEOUT` error to the agent, not `CancelledError`.

### Rule 5: SSH command cancellation

When a tool call is cancelled (e.g., agent disconnects or timeout):

1. The SSH channel running the command is closed
2. The tool returns `CONN_TIMEOUT` with `detail: {"reason": "cancelled"}`
3. The SSH connection itself is NOT torn down — only the channel
4. The persistent SSH connection remains usable for subsequent calls

---

# Retry Policy

## v0.1 Philosophy: No Automatic Retries

The MCP server follows a **fail-fast** philosophy. There are NO automatic retries at the tool level for v0.1.

When a tool call fails, it returns immediately with an error code. The calling agent decides whether to retry.

This applies to:
- SSH command execution failures → return `CONN_FAILED` or `CONN_TIMEOUT`
- Docker daemon unavailability → return `DOCKER_UNAVAILABLE`
- Command timeouts → return `CONN_TIMEOUT`
- Validation failures → return `VALID_*` errors

### What IS retried: SSH Connection Lifecycle

The SSH **connection** (not individual commands) has a limited reconnection policy:

| Attempt | Action                         | Wait before retry |
| ------- | ------------------------------ | ----------------- |
| 1       | Immediate reconnect            | 0 seconds         |
| 2       | Wait, then reconnect           | 5 seconds          |
| 3       | Wait, then reconnect           | 15 seconds         |
| 4+      | Background reconnect every 30s | 30 seconds         |

This is NOT a retry — it is connection lifecycle management. All pending tool calls still receive `CONN_RECONNECTING` errors.

### Rationale

- **Deterministic behavior**: The agent always knows why a call failed. No hidden retries introduce unpredictable latency.
- **Simplicity**: Retry logic at the wrong layer causes duplicate operations (e.g., restart requests).
- **Agent autonomy**: The LLM agent is better positioned to decide whether to retry, adjust parameters, or report to the human.
- **Auditability**: Every call has exactly one result. No "did it retry 3 times?" ambiguity.

### Future Consideration

Tool-level retries may be evaluated in v0.2 for idempotent read-only operations (e.g., `system_cpu_usage`), with:
- Maximum 2 retries
- Exponential backoff (1s, 3s)
- Only for `CONN_FAILED` and `CONN_TIMEOUT` errors
- Never for validation errors

---

# Command Execution Policy

## ALLOWED_COMMANDS Whitelist

The infrastructure layer enforces a whitelist of allowed commands. This is the security boundary from spec 004.

```python
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
    "vcgencmd_measure_temp": "vcgencmd measure_temp",
    "systemctl_status": "systemctl status",
    "systemctl_list": "systemctl list-units --type=service",
    "hostname": "hostname",
    "uptime": "uptime",

    # Logs
    "journalctl": "journalctl",

    # Workspace-scoped Docker Compose (executed from workspace directory)
    "compose_ps": "docker compose ps --format json",
    "compose_config": "docker compose config",
    "compose_logs": "docker compose logs",
    "compose_services": "docker compose config --services",
}
```

No command accepts raw user input as arguments. All parameters are validated and interpolated safely:

```python
# FORBIDDEN: passing raw user input
def run_command(cmd: str) -> str: ...

# REQUIRED: parameterized safe execution
def run_docker_logs(container: str, tail: int) -> str:
    # container is validated against container name regex
    # tail is validated as int in range 1-500
    # command is constructed from template, not concatenated from input
```

### Parameter Validation Rules

- Container names: must match `[a-zA-Z0-9][a-zA-Z0-9_.-]+`
- Port numbers: must be int in range 1-65535
- File paths: must resolve to an allowed directory, no path traversal (`..` forbidden)
- Line counts: must be int in range 1-500
- Service names: must match `[a-zA-Z0-9_.-]+`
- Workspace identifiers: must match `[a-zA-Z0-9_-]+` and be registered in `workspaces.yaml`

Any parameter that fails validation returns `VALID_PARAM_INVALID` error BEFORE any SSH command is executed.

### Workspace-Scoped Command Execution

Workspace tools execute commands from a specific working directory on the remote host. This is how `docker compose` commands are scoped to a project.

```python
# FORBIDDEN: agent-provided path
async def run_in_directory(cmd: str, cwd: str) -> str:  # cwd from agent input

# REQUIRED: workspace-resolved path
async def run_in_workspace(command_key: str, workspace_id: str, **params) -> str:
    # workspace_id is validated against the registry
    # path is resolved from workspace_id internally
    # command is constructed from ALLOWED_COMMANDS template
    # cwd is set to the workspace's resolved path
```

The `cwd` parameter for workspace commands is NEVER provided by the agent. It is ALWAYS resolved from the workspace registry. This is the security boundary for workspace isolation: the agent cannot choose which directory to execute from.

---

# Logging Strategy

## Library: structlog

All logging uses `structlog` with structured output.

## Configuration

```python
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),  # JSON to stderr
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),  # stderr
    cache_logger_on_first_use=True,
)
```

## Output Target

All MCP server logs go to **stderr**. This is critical because stdout is reserved for MCP JSON-RPC messages.

## Log Levels

| Level   | Usage                                                   |
| ------- | ------------------------------------------------------- |
| DEBUG   | SSH command execution details, raw command output       |
| INFO    | Tool calls, connection events, reconnection attempts    |
| WARNING | Parameter validation failures, degraded functionality  |
| ERROR   | SSH connection failures, command execution errors       |

## Structured Logging Schema

All log entries MUST include a minimum set of mandatory fields. Additional fields are encouraged but optional.

### Mandatory Fields

Every log entry MUST contain all of these fields:

| Field          | Type     | Description                                          |
| -------------- | -------- | ---------------------------------------------------- |
| `timestamp`    | str      | ISO 8601 UTC timestamp (e.g., `2025-01-15T14:30:00Z`) |
| `level`        | str      | Log level: `debug`, `info`, `warning`, `error`       |
| `event`        | str      | Event name (e.g., `tool_call`, `ssh_connect`, `ssh_disconnect`) |
| `tool`         | str      | Tool name when applicable (e.g., `docker_list_containers`) |
| `host`         | str      | Target SSH host (e.g., `oranpi.local`)               |
| `duration_ms`  | int      | Operation duration in milliseconds                    |
| `status`       | str      | `success` / `error` / `partial`                      |
| `error_code`   | str | None| Namespaced error code when status is `error` (e.g., `CONN_FAILED`) |

### Example Log Entries

**Successful tool call:**

```json
{
  "timestamp": "2025-01-15T14:30:00Z",
  "level": "info",
  "event": "tool_call",
  "tool": "docker_list_containers",
  "host": "cerra@oranpi.local",
  "duration_ms": 245,
  "status": "success",
  "error_code": null
}
```

**Failed tool call:**

```json
{
  "timestamp": "2025-01-15T14:31:05Z",
  "level": "error",
  "event": "tool_call",
  "tool": "docker_container_logs",
  "host": "cerra@oranpi.local",
  "duration_ms": 12,
  "status": "error",
  "error_code": "DOCKER_NOT_FOUND",
  "detail": {"container": "nginx-nonexistent"}
}
```

**SSH connection event:**

```json
{
  "timestamp": "2025-01-15T14:28:10Z",
  "level": "info",
  "event": "ssh_connect",
  "tool": null,
  "host": "cerra@oranpi.local",
  "duration_ms": 182,
  "status": "success",
  "error_code": null
}
```

**SSH reconnection attempt:**

```json
{
  "timestamp": "2025-01-15T14:35:22Z",
  "level": "warning",
  "event": "ssh_reconnect_attempt",
  "tool": null,
  "host": "cerra@oranpi.local",
  "duration_ms": 0,
  "status": "error",
  "error_code": "CONN_FAILED",
  "attempt": 2,
  "max_attempts": 3
}
```

### Validation

The `structlog` configuration pipeline enforces that all mandatory fields are present. Missing mandatory fields at log emission time MUST raise a `ValueError` in development mode and MUST be silently filled with `null` in production mode.

## Sensitive Data

Logs MUST NOT contain:
- SSH private key contents
- Container environment variable values
- Passwords or tokens
- Full file contents (only metadata like path, size, line count)

---

# OpenCode Integration

## MCP Server Configuration

In `opencode.json` (or `opencode.jsonc`), register the MCP server:

```json
{
  "mcp": {
    "servers": {
      "oranpi": {
        "command": "uv",
        "args": ["run", "--project", "C:\\Users\\cerra\\codigo\\MCP_Oranpi", "mcp-oranpi"],
        "env": {
          "ORANPI_SSH_HOST": "oranpi.local",
          "ORANPI_SSH_USER": "cerra",
          "ORANPI_SSH_KEY_PATH": "C:\\Users\\cerra\\.ssh\\id_oranpi"
        }
      }
    }
  }
}
```

Alternatively, if the environment variables are in `.env`:

```json
{
  "mcp": {
    "servers": {
      "oranpi": {
        "command": "uv",
        "args": ["run", "--project", "C:\\Users\\cerra\\codigo\\MCP_Oranpi", "mcp-oranpi"]
      }
    }
  }
}
```

## Entry Point

The `pyproject.toml` must declare a console script entry point:

```toml
[project.scripts]
mcp-oranpi = "mcp_oranpi.server:main"
```

The `server.py:main` function:

1. Loads configuration from environment variables (via `config.py`)
2. Establishes SSH connection
3. Registers all MCP tools
4. Starts stdio server lifecycle
5. On shutdown: closes SSH connection cleanly

## Tool Discovery

OpenCode automatically discovers MCP tools from the server. The agent sees all v0.1 audit tools as callable functions with their schemas.

The agent does NOT need to know about SSH, Docker, or the OrangePi directly. It calls `docker_list_containers` and receives structured results.

---

# Pyproject.toml Dependencies

```toml
[project]
name = "mcp-oranpi"
version = "0.1.0"
description = "Secure MCP server for OrangePi infrastructure auditing"
requires-python = ">=3.13"
dependencies = [
    "mcp>=1.0.0",           # MCP SDK
    "asyncssh>=2.17.0",     # Async SSH
    "pydantic>=2.0",        # Type validation
    "pydantic-settings>=2.0", # Typed settings from env
    "structlog>=24.0",      # Structured logging
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-mock>=3.14",
    "mypy>=1.11",
    "ruff>=0.8",
]

[project.scripts]
mcp-oranpi = "mcp_oranpi.server:main"

[tool.mypy]
strict = true

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "ANN", "S", "B", "A", "SIM"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

---

# Startup and Shutdown Lifecycle

```
STARTUP:
  1. Load config from environment → AppConfig
  2. Load workspace registry from workspaces.yaml
  3. Validate config (host, user, key_path exist and key is readable)
  4. Validate workspace paths against root directory
  5. Configure structlog
  6. Connect SSH (asyncssh.connect)
  7. Verify connection (execute: echo OK)
  8. Validate workspace directories over SSH (mark disabled/active/unreachable)
  9. Register MCP tools
  10. Start stdio server
  11. Log: "mcp-oranpi connected to {host} as {user} with {active_count}/{total_count} workspaces"

SHUTDOWN:
  1. Receive shutdown signal
  2. Close SSH connection gracefully
  3. Log: "mcp-oranpi shutting down"
  4. Exit 0

ERROR DURING STARTUP:
  - Missing config → Log error, exit 1
  - SSH key not readable → Log error, exit 1
  - SSH connection failed → Log error, exit 1 with retry hint
  - No valid workspaces → Log warning, continue (host-level tools still work)
  - Workspace path escapes root → Log warning, mark workspace as disabled, continue
```

---

# Risks and Tradeoffs

## Risk: Single SSH connection is a single point of failure

If the SSH connection drops, all tool calls fail until reconnection.

**Mitigation**: Reconnection policy (3 retries, then background retry every 30s). Tools return `CONN_FAILED` errors that agents can act on.

**Acceptance**: The single-target, single-user nature of this MCP makes connection pooling unnecessary. The reconnection policy covers transient failures.

## Risk: Windows SSH key path compatibility

The dev machine is Windows. SSH key paths use Windows format (`C:\Users\...`) but asyncssh expects the key to be readable.

**Mitigation**: `config.py` resolves `~` and validates that `key_path` exists and is readable at startup. Document that OpenSSH-compatible keys work (PEM format, not PuTTY .ppk).

**Known issue**: Windows users who generate keys with PuTTY need to convert to OpenSSH format. Document this in README.

## Risk: MCP stdio conflates stderr and logs

The MCP protocol requires JSON-RPC on stdout. Our logs go to stderr. If a library accidentally prints to stdout, it corrupts the MCP protocol.

**Mitigation**: `structlog` configured to use stderr. No `print()` calls anywhere in the codebase (Ruff rule enforced). MCP SDK manages stdout exclusively.

## Tradeoff: uv run as MCP command vs installed package

Using `uv run` means the MCP server starts via `uv run mcp-oranpi`. This adds ~1-2 seconds startup time for UV to sync dependencies.

**Alternative**: Install via `uv tool install` and reference the installed binary directly. Faster startup but requires manual updates.

**Decision**: Use `uv run` for development simplicity. Document the faster alternative for production use.

## Tradeoff: Command timeout of 30 seconds

Some commands (port scans, large docker logs) may take longer than 30 seconds.

**Mitigation**: `ORANPI_SSH_COMMAND_TIMEOUT` is configurable. The `logs_file` and `docker_container_logs` tools use `tail` to limit output, which also limits execution time. Port scans should use focused ranges.

**Accepted tradeoff**: 30 seconds is a reasonable default. Adjust via env var if needed.

## Tradeoff: Allowlist-based log file access

`logs_file` restricts paths to an allowlist. This prevents arbitrary file reads but requires updating `ORANPI_ALLOWED_LOG_DIRS` if new paths are needed.

**Accepted tradeoff**: Security over convenience. Adding a directory to the env var is a simple configuration change, not a code change.

## Risk: OrangePi ARM-specific commands

Some system tools (`vcgencmd`, `sensors`) are ARM-specific and may not exist on other hosts.

**Mitigation**: Each system tool should gracefully handle missing commands and return `SYS_SENSORS_UNAVAILABLE` or similar errors rather than crashing. The SSH client should catch `CommandNotFound`-style errors from the remote host.

## Risk: Workspace path escape

If the workspace registry contains a symlink that resolves outside `ORANPI_ROOT_WORKSPACE_DIR`, a workspace-scoped tool could execute commands from an unintended directory.

**Mitigation**: All workspace paths are resolved (symlinks followed) at startup. If the resolved path escapes the root directory, the workspace is marked `disabled` with `WS_PATH_ESCAPE`. The `workspace_resolver.py` module enforces this check before any command execution.

## Risk: Workspace registry out of sync with OrangePi

The `workspaces.yaml` file is local to the MCP repository and version-controlled. If the OrangePi directory structure changes (a project is renamed, moved, or deleted), the registry becomes stale.

**Mitigation**: Workspace validation runs at startup and detects stale entries. Stale workspaces are marked `disabled`, not removed. The agent sees the disabled status and can inform the human. Updating `workspaces.yaml` is a simple version-controlled change.

## Tradeoff: Workspace config local vs remote

Keeping `workspaces.yaml` in the MCP repository (on the dev machine) rather than on the OrangePi means workspace changes require a git commit and MCP restart. An alternative would be to store the registry on the OrangePi and fetch it over SSH.

**Accepted tradeoff**: Local version control is preferred because:
- Changes are auditable via git history
- The MCP server needs config before SSH connection is established
- It avoids a chicken-and-egg problem (need SSH to get config, need config to know what to do)
- A restart is a reasonable cost for workspace changes that happen infrequently

## Tradeoff: Docker Compose via cwd vs label filtering

Docker Compose commands execute from the workspace directory (`docker compose ps` from `/home/cerra/codigo/guardian/`). An alternative would be `docker ps --filter label=com.docker.compose.project=guardian` without changing directory.

**Accepted tradeoff**: The cwd approach is simpler and more reliable because:
- Docker Compose project names default to the directory name but can be overridden
- `docker compose ps` from the correct directory always shows the right project
- Label filtering requires knowing the project name, which may not match the directory name
- The cwd is resolved from the workspace registry, never from agent input

The cwd is set programmatically by `workspace_resolver.py`, never by the agent.