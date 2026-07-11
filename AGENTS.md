# AGENTS.md

## Purpose

This repository contains a secure MCP server for infrastructure auditing and controlled operations over remote OrangePi edge nodes.

The system is designed for:

* infrastructure auditing
* Docker inspection (host-level and workspace-scoped)
* network inspection
* observability
* deployment suggestions
* human-approved operations

This system is NOT an autonomous infrastructure controller.

The AI agent must operate under strict safety and operational boundaries.

---

# Agent Usage & Context Noise Reduction

To maintain workspace stability and prevent context window exhaustion, agents MUST strictly follow these usage rules:

1. **Explicit Request Only**: Do not invoke MCP OranPi tools proactively or redundantly without a direct user request or a specific, clear debugging/observability objective.
2. **No Polling Loops**: Avoid repeated, automatic tool invocations to poll logs, container stats, or system metrics. If a metric must be checked, run the check once and report back.
3. **Protect the Context Window**: Do not dump large raw payloads into the conversation logs. Always filter, slice, or truncate logs and JSON outputs to the minimum required for the task.
4. **Prefer Read-Only Observability**: Focus on read-only diagnostics (`_inspect`, `_list`, `_status`, `_usage`). Do not recommend or suggest infrastructure mutations unless explicitly prompted.

---

# Mandatory Reading Order

Before performing any implementation, the agent MUST read ALL of these specs:

1. `docs/Spec/001-product-context.md` — Product goals, non-goals, human-in-the-loop philosophy
2. `docs/Spec/002-architecture.md` — High-level architecture, tech stack, SSH layer, audit/admin split
3. `docs/Spec/003-engineering-standards.md` — Python 3.13+, UV, mypy strict, Ruff, async-first, testing
4. `docs/Spec/004-security-boundaries.md` — Forbidden operations, allowed commands, trust model
5. `docs/Spec/005-mcp-tool-contracts.md` — Tool schemas, error codes, truncation policy, metadata conventions
6. `docs/Spec/006-runtime-and-connectivity.md` — SSH config, project structure, env vars, logging, asyncio rules
7. `docs/Spec/007-testing-strategy.md` — Unit/integration/E2E test categories, fixtures, security tests
8. `docs/Spec/008-workspace-governance.md` — Workspace isolation, path validation, project scoping

The agent must fully understand all documents before proposing implementation.

---

# Tool Model

The MCP server exposes two levels of tools:

## Host-Level Tools (read-only)

Infrastructure observability across the entire OrangePi:

| Prefix      | Category  | Examples                                          |
| ----------- | --------- | ------------------------------------------------- |
| `docker_`   | Docker    | `docker_list_containers`, `docker_inspect_container`, `docker_container_logs` |
| `network_`  | Network   | `network_scan_ports`, `network_inspect_bindings`, `network_tailscale_status` |
| `system_`   | System    | `system_cpu_usage`, `system_memory_usage`, `system_disk_usage` |
| `logs_`     | Logs      | `logs_docker`, `logs_systemd`, `logs_file`        |

## Workspace-Scoped Tools (read-only)

Project-scoped inspection within registered workspaces:

| Prefix        | Category   | Examples                                          |
| ------------- | ---------- | ------------------------------------------------- |
| `workspace_`  | Workspace  | `workspace_list`, `workspace_inspect`, `workspace_docker_ps`, `workspace_logs`, `workspace_ports` |

The agent references workspaces by **logical identifier** (e.g. `"guardian"`), never by raw filesystem paths. The MCP resolves paths internally from `workspaces.yaml`.

---

# Error Code Namespace

All errors use `CATEGORY_SPECIFIC_ERROR` format:

| Prefix    | Category     | Examples                                        |
| --------- | ------------ | ----------------------------------------------- |
| `CONN_`   | Connection   | `CONN_FAILED`, `CONN_TIMEOUT`, `CONN_RECONNECTING` |
| `DOCKER_` | Docker       | `DOCKER_UNAVAILABLE`, `DOCKER_NOT_FOUND`, `DOCKER_NOT_RUNNING` |
| `NET_`    | Network      | `NET_SCAN_TIMEOUT`, `NET_SCAN_RANGE_INVALID`, `NET_TAILSCALE_NOT_INSTALLED` |
| `SYS_`    | System       | `SYS_SERVICE_NOT_FOUND`, `SYS_SENSORS_UNAVAILABLE` |
| `LOG_`    | Logs         | `LOG_PATH_FORBIDDEN`, `LOG_FILE_NOT_FOUND`, `LOG_FILE_OVERSIZED` |
| `WS_`     | Workspace    | `WS_NOT_FOUND`, `WS_DISABLED`, `WS_UNREACHABLE`, `WS_NO_COMPOSE`, `WS_PATH_ESCAPE` |
| `VALID_`  | Validation   | `VALID_PARAM_INVALID`, `VALID_PARAM_OUT_OF_RANGE`, `VALID_PAYLOAD_EXCEEDED` |
| `AUTH_`   | Authorization | `AUTH_FORBIDDEN`, `AUTH_HOST_KEY_REJECTED`      |

---

# Output Truncation Policy

| Metric                    | Limit  | Configurable                          |
| ------------------------- | ------ | ------------------------------------- |
| JSON response payload     | 1 MB   | `ORANPI_MAX_PAYLOAD_KB` (default 1024) |
| Raw log/text output       | 50 KB  | `ORANPI_LOG_OUTPUT_LIMIT_KB` (default 50) |
| List length (containers, ports, services) | 500 items | Hardcoded                    |
| Single field value        | 100 KB | Hardcoded                              |

---

# Workspace Governance

The OrangePi hosts multiple independent projects. The MCP MUST maintain workspace isolation:

* Only registered workspaces (defined in `workspaces.yaml`) are accessible
* The agent NEVER provides raw filesystem paths — only logical identifiers
* All workspace paths are validated at startup against `ORANPI_ROOT_WORKSPACE_DIR`
* Paths that escape the root directory are rejected with `WS_PATH_ESCAPE`
* Symlinks are resolved before allowlist checking
* Docker Compose commands execute from the workspace directory (resolved internally)
* The agent MUST NEVER assume ownership of the entire host

---

# Mandatory Workflow

The agent MUST follow this sequence:

1. Read all specs
2. Summarize understanding
3. Identify missing context or ambiguities
4. Ask clarification questions if needed
5. Wait for explicit approval
6. Only then begin SDD/TDD implementation

The agent MUST NOT start coding immediately after reading specs.

---

# Operational Philosophy

This project follows an:

* audit-first
* human-in-the-loop
* non-destructive
* security-constrained
* workspace-scoped

architecture philosophy.

The agent exists to:

* observe
* analyze
* suggest
* report

The human operator decides:

* deployments
* destructive actions
* infrastructure modifications
* approvals

---

# Safety Rules

The agent MUST NEVER:

* execute destructive Docker commands
* remove containers
* stop unrelated services
* kill processes blindly
* expose ports automatically
* disable firewalls
* modify SSH configuration
* run arbitrary shell input
* execute unvalidated commands
* accept arbitrary filesystem paths from agent input
* traverse parent directories (`..`)
* escape the workspace root directory

The agent MUST NEVER assume ownership of the host.

The OrangePi host may contain multiple critical projects running simultaneously.

---

# SSH and Runtime Configuration

Key environment variables:

| Variable                        | Required | Default         | Description                               |
| ------------------------------- | -------- | --------------- | ----------------------------------------- |
| `ORANPI_SSH_HOST`               | Yes      | —               | OrangePi hostname or IP                   |
| `ORANPI_SSH_USER`               | Yes      | —               | SSH username                              |
| `ORANPI_SSH_KEY_PATH`           | Yes      | —               | SSH private key path                      |
| `ORANPI_SSH_PORT`               | No       | 22              | SSH port                                  |
| `ORANPI_SSH_SECURITY_MODE`     | No       | production      | `production` or `development`            |
| `ORANPI_ROOT_WORKSPACE_DIR`     | Yes      | —               | Root dir for all workspaces on OrangePi   |
| `ORANPI_WORKSPACE_CONFIG`       | No       | workspaces.yaml | Path to workspace registry file           |
| `ORANPI_LOG_LEVEL`              | No       | INFO            | Log level                                 |
| `ORANPI_ALLOWED_LOG_DIRS`       | No       | /var/log,/home,/opt | Allowed dirs for logs_file            |
| `ORANPI_LOG_OUTPUT_LIMIT_KB`   | No       | 50              | Max log output in KB per response         |
| `ORANPI_MAX_PAYLOAD_KB`         | No       | 1024            | Max JSON payload in KB per response       |
| `ORANPI_MAX_LOG_FILE_MB`        | No       | 50              | Max readable log file size in MB          |

SSH security modes:
* **production** (default): strict host key verification, key permission check enforced, min INFO log level
* **development**: accept_new host keys, warn on key permissions, DEBUG allowed

Retry policy: No automatic retries at tool level. Fail-fast. Only SSH connection lifecycle has reconnection logic (3 retries then background every 30s).

Asyncio: `CancelledError` MUST always be re-raised. Never swallow. Use `asyncio.timeout` for command timeouts.

---

# Infrastructure Context

The target infrastructure:

* runs multiple Docker projects simultaneously
* uses Tailscale for remote secure access
* uses SSH keys authentication
* does NOT expose public SSH ports
* may optionally use Cloudflare Tunnel
* prioritizes security over automation

---

# Engineering Principles

The repository enforces:

* strict typing
* mypy strict
* Ruff
* UV package management
* async-first design
* clean architecture (hexagonal: domain → application ← infrastructure, presentation)
* structured logging (structlog to stderr, all MCP output on stdout)
* deterministic behavior
* workspace isolation (registered workspaces only, no arbitrary paths)

Project structure:

```
src/mcp_oranpi/
├── server.py              # MCP server entry point (stdio)
├── config.py              # Typed settings (AppConfig with SSHConfig + workspace config)
├── domain/                # Core models, errors, contracts, workspace registry
├── application/           # Tool implementations (docker_, network_, system_, logs_, workspace_)
├── infrastructure/        # SSH client, command runner, parsers, workspace resolver
└── presentation/          # MCP handler wiring
```

---

# Human Approval Requirements

Any mutative or potentially destructive operation MUST require explicit human approval.

Phase 1 (current): All tools are read-only except for workspace deployment.

Phase 2 (future): Admin tools require a confirmation token model:
1. Agent calls an admin tool
2. MCP returns `PENDING_APPROVAL` with a unique token
3. Human reviews and calls `admin_approve` or `admin_reject`
4. Only then does the MCP execute the operation

Audit-only operations are allowed without approval.

---

# Secure SSH Gatekeeper & Controlled Deployments

The system utilizes a server-side SSH Gatekeeper (`secure_gatekeeper.sh`) configured in the remote host's `~/.ssh/authorized_keys` for the agent's restricted SSH key.

This ensures that even if an agent attempts to bypass client-side code restrictions, the server blocks any command that is not explicitly whitelisted.

### Mutative Operations (Deployments)
- The only allowed mutative tool is `workspace_deploy`.
- It executes `deploy <project_name>` on the server.
- The server-side script intercepts this, navigates to the project directory under `/home/gonzalo/Gonzalo_codigo/`, performs a `git pull origin main`, and runs `docker compose up --build -d` for the whitelisted workspace.
- Any other mutative operation (like stopping containers, removing volumes, deleting networks) is hard-blocked by the server-side gatekeeper and returns `AUTH_FORBIDDEN` or command failure.


---

# Definition Of Done

A task is considered complete only if:

* tests pass
* mypy passes
* Ruff passes
* architecture boundaries are respected
* security boundaries are respected
* workspace isolation is maintained
* no forbidden operations were introduced
* implementation matches specs exactly

---

# Implementation Priority

Priority order:

1. Safety
2. Observability
3. Determinism
4. Maintainability
5. Performance
6. Convenience

Never sacrifice safety for convenience.

---

# Project Tools — Vulnerability Scanner

Before or after making code changes, run the vulnerability scanner:

```bash
uv run python scripts/vulnerability_scanner.py              # Full scan (28 rules)
uv run python scripts/vulnerability_scanner.py --severity high   # Only CRITICAL + HIGH
uv run python scripts/vulnerability_scanner.py --category security  # Security only
uv run python scripts/vulnerability_scanner.py --json        # JSON output for CI/CD
```

Exit code 0 = clean, 1 = findings. Use `--severity high` as a CI gate.

**28 rules across 6 categories:**
- **SEC-001 to SEC-012**: Security (hardcoded secrets, auth bypass, XSS, CSRF, deps CVE, log leaks, redirects)
- **CONC-001 to CONC-002**: Concurrency (race conditions, sync-blocking-in-async, session per request)
- **ARCH-001 to ARCH-002**: Architecture (hexagonal violations, session instantiation outside DI)
- **DI-001**: Dependency injection (manual repo instantiation in routes)
- **AP-001 to AP-004**: Anti-patterns (broad except, Any, mutable defaults, insecure random)
- **SMELL-001 to SMELL-002**: Code smells (`is` with literals, print in production)
- **RES-001 to RES-003**: Resources (context managers, silent failures, DB connection leaks)
- **CONF-001 to CONF-002**: Configuration (algo mismatch, .env drift)

Also available: `uv run python scripts/check_architecture.py` for hexagonal dependency linting.