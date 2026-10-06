# MCP OranPi 🍊

Secure read-only MCP (Model Context Protocol) server for remote infrastructure auditing on an **OrangePi 5 Plus**.

Designed for AI agents to observe, analyze, and report — never to mutate or destroy.

---

## Table of Contents

- [Architecture](#architecture)
- [Security Model](#security-model)
- [Quick Start](#quick-start)
- [SSH Key Strategy](#ssh-key-strategy)
- [Server-Side Gatekeeper](#server-side-gatekeeper)
- [Available Tools](#available-tools)
- [Workspaces](#workspaces)
- [Configuration](#configuration)
- [Development](#development)
- [Quality Gates](#quality-gates)

---

## Architecture

```
Hexagonal (Clean Architecture)
─────────────────────────────────
           presentation/       ← MCP handlers, stdio server
                │
           application/        ← Tool implementations (read-only)
              ╱   ╲
        domain/   infrastructure/  ← SSH client, parsers, workspace resolver
```

- **Python 3.13+** — async-first throughout
- **UV** — fast Python package manager
- **asyncssh** — async SSH client with reconnection
- **pydantic v2** — typed configuration and validation
- **structlog** — structured logging (stderr, never interferes with MCP stdout)

### Layer Rules

| Layer | Imports From | Responsibility |
|-------|-------------|----------------|
| `domain/` | nothing | Models, errors, contracts, validation, truncation, redaction |
| `application/` | domain, infrastructure | Tool implementations (read-only orchestration) |
| `infrastructure/` | domain | SSH client, command runner, parsers, workspace resolver |
| `presentation/` | application, infrastructure | MCP handler wiring, server lifecycle |

**DI exception**: `application → infrastructure` is allowed for `CommandRunner`, parsers, and `WorkspaceResolver`.

---

## Security Model

Three layers of defense, from client to server:

### Layer 1: Application-Level (MCP code)

- **All tools are read-only** — no `docker rm`, `docker stop`, `docker prune`, `kill`, or any destructive commands
- **Secret redaction** — API keys, tokens, passwords, `.env`, `.pem`, `.key`, and sensitive paths (`.ssh/`, `.cloudflared/`, `.aws/`) are stripped from ALL tool output via `redact_json_response()`
- **Path validation** — log file access restricted to `/var/log/`, `/home/`, `/opt/`; blocked paths for known secret locations
- **Output truncation** — payloads capped at 1 MB, logs at 50 KB, lists at 500 items

### Layer 2: SSH Gatekeeper (Server-Side)

Each SSH connection via the agent key is wrapped by `secure_gatekeeper.sh` on the OrangePi.
Only whitelisted commands pass through. Everything else returns `Acceso denegado: Comando no autorizado.`

See [Server-Side Gatekeeper](#server-side-gatekeeper) for details.

### Layer 3: SSH Key Separation

| Key | Purpose | Gatekeeper |
|-----|---------|-----------|
| `id_ed25519` | Personal admin access (you) | ❌ Bypasses gatekeeper |
| `id_agente_ed25519` | MCP agent | ✅ Passes through gatekeeper |

---

## SSH Key Strategy

The MCP **must** use `id_agente_ed25519` (the `llave-agente-ia` key) to benefit from server-side gatekeeper protection.

### Setup

```bash
# Private key on your Windows machine:
C:\Users\cerra\.ssh\id_agente_ed25519
C:\Users\cerra\.ssh\id_agente_ed25519.pub
```

The public key is installed on the OrangePi's `~/.ssh/authorized_keys` with the gatekeeper wrapper:

```
command="/home/gonzalo/scripts/secure_gatekeeper.sh",
no-port-forwarding,no-x11-forwarding,no-agent-forwarding
ssh-ed25519 AAAAC3NzaC...PykLsh llave-agente-ia
```

### Switch Between Keys

Edit `.env` (gitignored, local only):

```
# Personal key (bypasses gatekeeper — admin only)
ORANPI_SSH_KEY_PATH=~/.ssh/id_ed25519

# Agent key (passes through gatekeeper — MCP default)
ORANPI_SSH_KEY_PATH=~/.ssh/id_agente_ed25519
```

---

## Server-Side Gatekeeper

Location on OrangePi: `/home/gonzalo/scripts/secure_gatekeeper.sh`

### Deploy Whitelist

Only these projects can be deployed (via `workspace_deploy` tool which returns manual steps):

| Project | Directory |
|---------|-----------|
| `pedidos_multi` | `Gonzalo_codigo/Pedidos_Multi/aplicacion_pedidos_multitenant` |
| `meli_bunker` | `Gonzalo_codigo/Meli_Bunker/Meli-Bunker` |
| `agente_oriental` | `Gonzalo_codigo/Agente_Oriental` |
| `agente_hibrido` | `Gonzalo_codigo/agente_hibrido` |
| `bot_discord` | `Gonzalo_codigo/bot_discord` |
| `api_statica` | `Gonzalo_codigo/api_statica` |

### Audit Command Whitelist

| Category | Commands |
|----------|----------|
| Docker | `docker ps`, `docker inspect`, `docker stats`, `docker port`, `docker logs` |
| Network | `ss -tulnp` |
| VPN | `tailscale status --json` |
| System | `top -bn1`, `free -m`, `df -h`, `df -T`, `uptime`, `hostname` |
| Services | `systemctl status`, `systemctl list-units`, `journalctl` |
| Temperature | Thermal zones (`/sys/class/thermal/`) + `vcgencmd` fallback |
| File Info | `stat`, `readlink` (scoped to safe paths) |
| Logs | `tail -n N` (max 500 lines, scoped to safe paths) |
| Docker Compose | `docker compose ps`, `docker compose config`, `docker compose logs` (scoped to workspaces) |

### Security Hardening

- Path traversal (`..`) blocked
- Shell injection characters (`;`, `|`, `&`, `>`, `<`, `$`, `` ` ``) blocked in variable arguments
- Log file reads restricted to `/var/log/`, `/home/`, `/opt/`
- `tail` limited to 500 lines

---

## Quick Start

```bash
# Clone and install
git clone <repo>
cd MCP_Oranpi
uv sync

# Configure
cp .env.example .env
# Edit .env with your OrangePi SSH details

# Run (stdio mode — for OpenCode / MCP clients)
uv run python src/mcp_oranpi/server.py

# Run tests
uv run pytest
uv run mypy src
uv run ruff check src
uv run python scripts/check_architecture.py
```

### OpenCode Configuration

Add to `opencode.json`:

```json
{
  "mcpServers": {
    "oranpi": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\cerra\\codigo\\MCP_Oranpi", "python", "src/mcp_oranpi/server.py"]
    }
  }
}
```

---

## Available Tools

All tools are **read-only**. 22 tools across 5 categories:

### Docker (5)

| Tool | Description |
|------|-------------|
| `docker_list_containers` | List containers (all or by filter) |
| `docker_inspect_container` | Full container metadata |
| `docker_container_stats` | CPU, memory, network IO |
| `docker_container_logs` | Logs with time filtering |
| `docker_inspect_ports` | Port bindings and occupancy |

### Network (4)

| Tool | Description |
|------|-------------|
| `network_scan_ports` | Scan ports via `ss -tulnp` |
| `network_inspect_bindings` | All active network bindings |
| `network_tailscale_status` | Tailscale VPN status |
| `network_suggest_port` | Suggest available ports near a preferred one |

### System (5)

| Tool | Description |
|------|-------------|
| `system_cpu_usage` | CPU snapshot + load averages |
| `system_memory_usage` | RAM + swap statistics |
| `system_disk_usage` | Disk usage per filesystem |
| `system_temperatures` | SoC/CPU/GPU/NPU temperatures |
| `system_service_status` | Systemd service status |

### Logs (3)

| Tool | Description |
|------|-------------|
| `logs_docker` | Docker logs (alias) |
| `logs_systemd` | Journalctl logs by unit/priority |
| `logs_file` | File logs with 7-step security validation |

### Workspace (5)

| Tool | Description |
|------|-------------|
| `workspace_list` | List registered workspaces |
| `workspace_inspect` | Workspace metadata + compose config |
| `workspace_docker_ps` | Compose containers in workspace |
| `workspace_logs` | Compose service logs |
| `workspace_ports` | Compose port mappings |
| `workspace_deploy` | **Read-only** — returns manual steps for human |

---

## Workspaces

Workspaces are registered in `workspaces.yaml` and resolved by the server at startup.

### Current Workspaces

| ID | Path |
|----|------|
| `meli_bunker` | `Gonzalo_codigo/Meli_Bunker/Meli-Bunker` |
| `guardian_rust` | `Gonzalo_codigo/Guardian/guardian_rust` |
| `pedidos_multi` | `Gonzalo_codigo/Pedidos_Multi/aplicacion_pedidos_multitenant` |
| `agente_oriental` | `Gonzalo_codigo/Agente_Oriental` |
| `api_statica` | `Gonzalo_codigo/api_statica` |
| `bot_discord` | `Gonzalo_codigo/bot_discord` |
| `agente_hibrido` | `Gonzalo_codigo/agente_hibrido` |

All paths are relative to `ORANPI_ROOT_WORKSPACE_DIR` (`/home/gonzalo/Gonzalo_codigo/`).

The agent references workspaces by **logical identifier**, never by raw filesystem paths.

---

## Configuration

Environment variables (`.env` — gitignored):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ORANPI_SSH_HOST` | Yes | — | OrangePi hostname or IP |
| `ORANPI_SSH_USER` | Yes | — | SSH username |
| `ORANPI_SSH_KEY_PATH` | Yes | — | SSH private key path |
| `ORANPI_SSH_PORT` | No | 22 | SSH port |
| `ORANPI_SSH_SECURITY_MODE` | No | production | `production` or `development` |
| `ORANPI_ROOT_WORKSPACE_DIR` | Yes | — | Root dir for workspaces |
| `ORANPI_LOG_LEVEL` | No | INFO | Log level |
| `ORANPI_ALLOWED_LOG_DIRS` | No | `/var/log,/home,/opt` | Allowed dirs for `logs_file` |
| `ORANPI_LOG_OUTPUT_LIMIT_KB` | No | 50 | Max log output per call |
| `ORANPI_MAX_PAYLOAD_KB` | No | 1024 | Max JSON payload per call |
| `ORANPI_MAX_LOG_FILE_MB` | No | 50 | Max readable log file size |

### SSH Security Modes

- **production** (default): strict host key verification, key permission check, INFO min log level
- **development**: accept new host keys, warn on permissions, DEBUG allowed

---

## Development

### Prerequisites

- Python 3.13+
- UV (`pip install uv`)

### Commands

```bash
# Install dependencies
uv sync

# Run all tests
uv run pytest

# Type checking (strict)
uv run mypy src

# Linting
uv run ruff check src

# Architecture boundary check
uv run python scripts/check_architecture.py

# Vulnerability scanner
uv run python scripts/vulnerability_scanner.py
uv run python scripts/vulnerability_scanner.py --severity high   # CI gate
```

### Project Structure

```
src/mcp_oranpi/
├── server.py              # MCP server entry point (stdio)
├── config.py              # Typed settings
├── domain/                # Core models, errors, contracts, validation, truncation, redaction
├── application/           # Tool implementations
├── infrastructure/        # SSH client, command runner, parsers, workspace resolver
└── presentation/          # MCP handler wiring

tests/
├── unit/                  # Unit tests
├── helpers.py             # Mocks (MockSSHClient, MockCommandRunner, MockWorkspaceResolver)
├── conftest.py            # Fixtures (clears ORANPI_* env vars)

scripts/
├── check_architecture.py  # Hexagonal dependency linter
├── vulnerability_scanner.py  # 28 security rules across 6 categories
├── test_oranpi_full.py    # Full 22-tool integration test
└── test_redaction_integration.py  # Secret redaction integration test
```

---

## Quality Gates

All must pass before release:

| Gate | Command |
|------|---------|
| ✅ Tests | `pytest` |
| ✅ Types | `mypy src` (strict) |
| ✅ Lint | `ruff check src` |
| ✅ Architecture | `scripts/check_architecture.py` |
| ✅ Security | `scripts/vulnerability_scanner.py --severity high` |

---

## Error Codes

All errors use `CATEGORY_SPECIFIC_ERROR` format:

| Prefix | Category |
|--------|----------|
| `CONN_` | Connection (SSH) |
| `DOCKER_` | Docker operations |
| `NET_` | Network scanning |
| `SYS_` | System commands |
| `LOG_` | Log operations |
| `WS_` | Workspace operations |
| `VALID_` | Parameter validation |
| `AUTH_` | Authorization |

---

## Infrastructure — LIVE STATUS

**OrangePi 5 Plus** — RK3588S, 16 GB RAM, 7 thermal zones

| Metric | Value |
|--------|-------|
| **Tailscale IP** | `100.106.85.109` |
| **Hostname** | `gonpatri.tail919aa9.ts.net` |
| **SSH** | `ssh gonzalo@100.106.85.109` |
| **CPU** | 4% (1 core detected) |
| **RAM** | 15.7 GB total, 38% used |
| **Disk** | 1.8 TB, 13% used |
| **Temperature** | 30-31°C (SoC, bigcore, gpu, npu) |
| **Uptime** | ~16 days (1,375,140 seconds) |

### Active Containers (23 total)

| Container | Status | Ports |
|----------|--------|-------|
| svl-app (meli_bunker) | Up 3h, healthy | 8000 |
| svl-db (meli_bunker) | Up 2w, healthy | 5433 |
| svl-nginx (meli_bunker) | Up 2w, healthy | 8002 |
| aplicacion_pedidos_multitenant-* | Up 9-10 days | 5445, 8010 |
| agente_oriental-* | Up 2w, healthy | 8550, 8552 |
| agente_hibrido-* | Up 2w, healthy | 8501, 8005, 5436 |
| bot_discord-* | Up 2w | 8000, 8080, 8082, 5432 |
| bookgraph-neo4j | Up 2w, healthy | 7474, 7687 |
| api_seguros-* | Up 2w | 8001, 5050, 5440 |

### Registered Workspaces (7)

| ID | Path | Services |
|----|------|----------|
| `meli_bunker` | Meli_Bunker/Meli-Bunker | app, db, nginx |
| `pedidos_multi` | Pedidos_Multi/aplicacion_pedidos_multitenant | app, db |
| `agente_oriental` | Agente_Oriental | app, guardian, ocr |
| `agente_hibrido` | agente_hibrido | frontend, backend, postgres |
| `bot_discord` | bot_discord | bot, pgadmin, nginx, postgres |
| `api_statica` | api_statica | — |
| `guardian_rust` | Guardian_Rust/Guardian_Orangpi5 | — |

**Cloudflare Tunnel**: Active on port 20241
**Tailscale**: Online, reachable

---

## Operational Philosophy

```
audit-first
human-in-the-loop
non-destructive
security-constrained
workspace-scoped
```

The agent **observes, analyzes, suggests, reports**.
The human **decides, deploys, approves**.

---

## FAQ

**Q: Why can't the MCP deploy things automatically?**
A: By design. `workspace_deploy` returns manual steps for a human operator. Destructive operations require human approval — no exceptions.

**Q: What happens if someone injects a malicious command?**
A: Three layers block it: (1) application code only sends whitelisted commands, (2) gatekeeper rejects unknown commands server-side, (3) redaction strips secrets from output.

**Q: Why two SSH keys?**
A: Separation of concerns. Your personal key bypasses the gatekeeper for admin tasks. The agent key is constrained by the gatekeeper. If the agent is compromised, the server still protects itself.

**Q: How do I add a new workspace?**
A: Add it to `workspaces.yaml`, add the deploy path to `secure_gatekeeper.sh` on the server, and restart the MCP server.
