# AGENTS.md — MCP OranPi Quick Reference

## What This Tool Does

**MCP OranPi** is a read-only infrastructure auditing server for an **OrangePi 5 Plus** running multiple Docker projects. It connects via Tailscale VPN and exposes **22 diagnostic tools**.

**Use it to**: diagnose problems, inspect containers, analyze logs, check resource usage, and report findings to the human operator.

**Do NOT use it to**: modify infrastructure, stop containers, delete anything, or make autonomous decisions.

---

## The 22 Tools

### Docker Inspection (5)
```
docker_list_containers    → List all containers (running or stopped)
docker_inspect_container  → Full details of ONE container
docker_container_stats    → CPU, memory, network, disk I/O
docker_container_logs     → Logs with optional time filtering
docker_inspect_ports      → Port bindings across containers
```

### Network Diagnostics (4)
```
network_scan_ports         → Scan port range (max 10000 ports)
network_inspect_bindings  → All active network listeners
network_tailscale_status  → Tailscale VPN status + peers
network_suggest_port       → Find available ports near a preferred one
```

### System Monitoring (5)
```
system_cpu_usage          → CPU %, per-core %, load averages
system_memory_usage       → RAM total/used/available + swap
system_disk_usage        → Disk space per mount point
system_temperatures       → SoC, CPU, GPU, NPU temperatures
system_service_status     → systemd services (list all or one)
```

### Log Access (3)
```
logs_docker               → Docker container logs (alias)
logs_systemd             → Journalctl by unit/priority
logs_file                → Read log files with security validation
```

### Workspace Scoped (5)
```
workspace_list            → List all registered workspaces
workspace_inspect         → Workspace metadata + compose services
workspace_docker_ps       → Containers in a specific workspace
workspace_logs           → Logs from a workspace's compose services
workspace_ports           → Port mappings in a workspace
```

### Deployment (1 - read-only)
```
workspace_deploy          → Execute git pull + docker compose up
                           → REQUIRES human approval before use
```

---

## Quick Diagnostic Workflow

When the user reports a problem:

```python
# 1. Check container status
docker_list_containers(filter="problem_container_name")

# 2. Inspect the container
docker_inspect_container("container_name")

# 3. Get logs
docker_container_logs("container_name", tail=50)

# 4. Check resources if it's running
docker_container_stats("container_name")

# 5. If it's a workspace issue
workspace_inspect("workspace_id")
workspace_logs("workspace_id", service="service_name", tail=50)

# 6. System-wide check
system_cpu_usage()
system_memory_usage()
system_temperatures()
```

---

## What NEVER To Do

- ❌ `docker stop`, `docker rm`, `docker kill`
- ❌ `docker compose down`, `docker compose stop`
- ❌ `systemctl stop`, `systemctl restart`
- ❌ Modify files, environment variables, ports
- ❌ Use raw filesystem paths (use workspace IDs)
- ❌ Poll metrics repeatedly (check once, report)
- ❌ Dump full JSON payloads into chat (summarize)

---

## Error Codes

| Prefix | Meaning |
|--------|---------|
| `CONN_*` | SSH connection failed/timeout |
| `DOCKER_*` | Docker daemon or container issue |
| `NET_*` | Network scan or Tailscale issue |
| `SYS_*` | System metrics (CPU, RAM, temps) |
| `LOG_*` | Log file access denied or not found |
| `WS_*` | Workspace not found or disabled |
| `VALID_*` | Invalid parameters |

---

## Connection Info

```
Host:     100.106.85.109 (Tailscale)
User:     gonzalo
Key:      ~/.ssh/id_agente_ed25519
SSH Port: 22
Workspaces root: /home/gonzalo/Gonzalo_codigo
```

---

## Example Diagnosis

**User says**: "The meli_bunker app is slow"

**Agent checks**:
```
1. docker_container_stats("svl-app")
   → CPU 0.29%, Memory 86MB - looks fine

2. docker_container_logs("svl-app", tail=100)
   → Shows repeated slow DB queries

3. docker_inspect_container("svl-db")
   → DB healthy, but high connection count

4. system_resource_check
   → RAM 38%, Disk 13% - healthy

**Report**: "The app container is fine. The slowdown is likely
the PostgreSQL DB (svl-db) having too many connections or slow
queries. Check DB logs and consider connection pooling."
```

---

## Rules

1. **Read-only by default** — all tools observe only
2. **Human approves deployments** — `workspace_deploy` needs explicit OK
3. **Workspace isolation** — always use logical IDs, never raw paths
4. **Summarize, don't dump** — filter output before showing user
5. **Fail fast** — report errors clearly with error codes
