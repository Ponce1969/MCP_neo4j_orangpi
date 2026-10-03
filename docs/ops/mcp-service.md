# MCP Service — Operator Runbook

Operational contract for the MCP SSE service on the OrangePi (`100.106.85.109`,
Tailscale; no public ports). This document is normative: read it before you
restart, debug, or verify the service. It never mutates the Neo4j graph.

## 1. Service identity

| Item | Value |
|------|-------|
| systemd unit | **`mcp-server.service`** (Description: `Book Graph RAG MCP Server`) |
| Live unit file | `/etc/systemd/system/mcp-server.service` |
| Repo copy | `deploy/mcp-server.service` |
| `User` | `gonzalo` |
| `WorkingDirectory` | this repo (`/home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi`) |
| `ExecStart` | `uv run book-graph-rag-mcp serve` (live unit: absolute `~/.local/bin/uv`) |
| `Environment` | `PATH=/home/gonzalo/.local/bin:/usr/local/bin:/usr/bin:/bin` (the only `Environment=` in the live unit) |
| Configuration source | the service reads `<repo>/.env` from its `WorkingDirectory` (pydantic-settings), **not** via `EnvironmentFile` |
| `Restart` | `on-failure` |
| `RestartSec` | `5` |
| State | `enabled` (starts on boot) |
| Ordering | `After=network.target docker.service`, `Wants=docker.service` |

**Why `Wants=docker.service`:** Neo4j runs as the Docker container
`bookgraph-neo4j` on this host. The unit asks systemd to bring Docker up before
the MCP server starts, because the service cannot answer a single query without
the graph.

**The unit name does not contain the project name.** That is the single most
important fact in this document.

### Repo copy vs the live unit (drift — resolved 2026-10-03, keep checking)

`deploy/mcp-server.service` in this repo was **not** what was installed until
2026-10-03. The difference was verified on that date:

| Setting | Live unit before the install | Repo `deploy/mcp-server.service` |
|---------|------------------------------|----------------------------------|
| `EnvironmentFile` | absent | `EnvironmentFile=<repo>/.env` |
| `Environment=MCP_BIND_HOST` | absent | `Environment=MCP_BIND_HOST=100.106.85.109` (with an R7 comment about a fail-closed private bind) |

While the drift lasted, the service still worked because it read `.env` from its
`WorkingDirectory`, and it still bound only the Tailscale interface because
`MCP_BIND_HOST` is set in `.env`. What was missing was the hardening: with the
repo copy installed, the Tailscale bind is declared in the unit itself, so a
`.env` mistake cannot silently point the service at a wildcard address.

The install landed on 2026-10-03 (unit `mtime` 15:34, 734 bytes) and the two files
are now byte-identical
(sha256 `23a13f72d59b91fed34ef6482d9b59ad89e94dd6713481ca68aed84b1c771500`). Even
so: **never assume the repo copy describes the running unit.** Read the live one.

Installing the repo copy is a maintainer action (an agent has no sudo password on
this host). Use an **absolute source path**: a relative one silently fails when the
shell is not in the repo, and the failure is easy to miss because
`daemon-reload && restart` still succeed afterwards:

```bash
sudo cp /home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi/deploy/mcp-server.service /etc/systemd/system/mcp-server.service
sudo systemctl daemon-reload && sudo systemctl restart mcp-server
```

### Confirming the artifact actually landed

`systemctl restart` says nothing about *which* file was installed, so prove it
before believing the hardening is in place:

```bash
ls -l --time-style=full-iso /etc/systemd/system/mcp-server.service   # mtime must be just now
diff <(cat deploy/mcp-server.service) <(systemctl cat mcp-server | tail -n +2) && echo identical
systemctl show mcp-server -p EnvironmentFiles -p Environment --no-pager  # must show the override
```

Two traps in that check:

- Do **not** filter comment lines (`grep -v '^#'`) when comparing: the artifact has
  explanatory comments, so stripping `#` lines from only one side reports a
  difference that does not exist. Drop only the leading `# /etc/systemd/system/...`
  path line that `systemctl cat` prepends (`tail -n +2`).
- A `cp` that never ran (wrong cwd, relative path, a `sudo` that failed) leaves the
  old file in place while the reload and restart still succeed: the service looks
  healthy and the private-bind hardening is silently missing. This is what happened
  on 2026-10-03 — the first attempt left the file's mtime at 2026-06-23 after a
  restart that came from a valid sudo session, and only the second attempt (absolute
  source path) landed.

## 2. Operating the service

Restart (sudo asks for a password on this host):

```bash
sudo systemctl restart mcp-server
```

Read-only commands — no sudo required:

```bash
systemctl status mcp-server
systemctl is-active mcp-server
systemctl cat mcp-server
```

> **Wrong name:** `systemctl status book-graph-rag-mcp` is not this unit; it
> answers `Unit book-graph-rag-mcp.service could not be found.` The *console
> script* is `book-graph-rag-mcp`; the *unit* is `mcp-server.service`.

Never control the service with a background `uv run` — see §3.

## 3. Warning — never launch a manual instance

**Warning:** launching `uv run book-graph-rag-mcp serve` by hand (shell
foreground, `&`, `nohup`, cron, tmux) causes the restart loop.

Mechanism: while any manual process holds `100.106.85.109:8003`, every unit
retry fails to bind, the unit dies, systemd retries after `RestartSec=5`, and
the cycle repeats forever — exactly what produced the historical
**43,558-restart** incident.

### Process signature of a restart loop

| Signal | Value |
|--------|-------|
| process pairs | `uv run` + `book-graph-rag-mcp serve` |
| `ppid` | `1` (reparented, transient) |
| `stdin` | `/dev/null` |
| `stdout` / `stderr` | `socketpair` (journal pipe, not a file) |
| environment | minimal unit-provided env (no interactive shell vars) |
| cwd | the repo (`WorkingDirectory`) |
| cadence | a new pair appears every ~5 s |

### Finding the real unit

```bash
systemctl list-units | grep -i mcp     # finds mcp-server.service
```

Checks that **miss** this unit (its name never contains the project):

| Check | Why it misses |
|-------|---------------|
| `systemctl status book-graph-rag-mcp` | wrong name → "could not be found" |
| `systemctl list-unit-files \| grep -i -E "book\|graph"` | unit file is `mcp-server.service` |
| `crontab -l` | the unit is not a cron job |
| `~/.config/systemd/user/` | it is a system unit in `/etc/systemd/system/`, not a user unit |
| grepping the command under `~/.config` | same reason: the unit lives in `/etc/systemd/system/` |

To clear a loop: kill the manual instance holding port 8003, then
`sudo systemctl restart mcp-server`, then verify per §4.

## 4. Verifying a deploy or restart

Smoke check (read-only, against production):

```bash
set -a; . ./.env; set +a      # exports MCP_ACCESS_TOKEN — never print its value
uv run --no-sync python scripts-ops/mcp_smoke_book4.py
```

A good run shows:

- handshake `book-graph-rag 1.28.0` (the version is the installed `mcp` SDK
  pinned in `uv.lock`, not this project's version);
- `count_entities` for the four namespaces, in the order the script prints
  them (baseline 2026-10-03):

  | Scope | Entities |
  |-------|----------|
  | `knowledge:agentic-architectural-patterns` | 7111 |
  | `knowledge:essential-graphrag` | 1241 |
  | `knowledge:graphrag-agentic` | 6078 |
  | `knowledge:ai-engineering-huyen` | 6963 |

- a `search_rag` sanity block plus one cross-scope isolation query;
- the tool list: `ask_global`, `count_entities`, `find_entity`, `list_entities`,
  `query_cypher`, `search_chunks`, `search_rag`, `traverse_relationships` —
  exactly **8 tools**.

### Reading the outcome

| Outcome | Meaning | Action |
|---------|---------|--------|
| every request `401` `{"error":"unauthorized",...}` | auth is working; the token was not exported | re-source `.env`, rerun. Never print the token value |
| handshake, counts, and 8 tools as above | healthy | done |
| connection refused / bind failure | service down, or a manual instance holds `:8003` | §3, then `systemctl status mcp-server` |
| any other error | real failure | read `journalctl -u mcp-server` first |

## 5. Restart required after catalog changes

`CatalogScopeResolver` loads `catalog.yaml` **once at construction** and caches
it for the process lifetime. After editing `catalog.yaml` (new/renamed source,
status change), the running service keeps serving the old catalog until:

```bash
sudo systemctl restart mcp-server
```

## 6. Logs

- The unit's `ExecStart` has no output redirection, so stdout/stderr go to the
  systemd journal: `journalctl -u mcp-server` (add `sudo` only if permissions
  deny it).
- The application also appends metadata-only records to
  `logs/mcp_queries.jsonl` (relative to `WorkingDirectory`): tool name, counts,
  durations, error codes, keyed HMAC fingerprints. Raw query text is never
  written (fail-closed fingerprinting); files older than 7 days are pruned.

## 7. Related rules

- Graph mutation gate (AGENTS.md §7.2): fresh backup → dry-run → human approval
  → apply → audit. This runbook is read-only with respect to the graph.
- AGENTS.md §7: this host runs 23 production containers from unrelated
  projects. Touch only `bookgraph-neo4j`; never prune, remove, or restart
  anything else.
