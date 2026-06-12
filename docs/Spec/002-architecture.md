# 002 - Architecture

## High-Level Architecture

```text
OpenCode Agent
        │
        ▼
Local MCP Server
        │
        ▼
SSH Layer
        │
        ▼
OrangePi5 Plus
```

---

# Connectivity Model

## Local Access

Inside the local network:

* SSH via local LAN IP
* SSH key authentication
* no password authentication

## Remote Access

Outside the local network:

* Tailscale secure mesh
* SSH over Tailscale
* no public SSH exposure

## Optional Connectivity

Cloudflare Tunnel may be used optionally for:

* dashboards
* web interfaces
* observability endpoints

Cloudflare Tunnel is NOT mandatory for SSH connectivity.

---

# Security Architecture

The architecture intentionally avoids:

* public SSH ports
* direct internet exposure
* unrestricted remote execution
* autonomous infrastructure control

Security is prioritized over convenience.

---

# MCP Design

The MCP server must:

* expose constrained tools
* validate all operations
* avoid arbitrary shell execution
* isolate infrastructure actions
* separate audit vs mutative capabilities

---

# Recommended MCP Separation

## audit_mcp

Read-only operations:

* Docker inspection
* logs
* metrics
* network inspection
* observability

## admin_mcp

Human-approved mutative operations:

* restarts
* deployments
* configuration changes

The audit MCP should be implemented first.

---

# Recommended Python Stack

* Python 3.13+
* asyncio
* asyncssh
* pydantic v2
* structlog
* pytest
* mypy
* Ruff
* UV

---

# Architectural Constraints

The system must:

* remain async-first
* maintain strict typing
* avoid blocking IO
* avoid hidden side effects
* provide deterministic outputs

---

# Command Execution Policy

Commands must:

* be predefined
* validated
* constrained
* audited

The system must never execute arbitrary user shell input.

---

# Infrastructure Awareness

The host may contain:

* multiple PostgreSQL instances
* multiple Redis instances
* multiple Docker Compose projects
* shared services

The MCP must never assume exclusive host ownership.
