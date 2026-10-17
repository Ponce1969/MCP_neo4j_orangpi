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
  them (baseline 2026-10-07):

  | Scope | Entities |
  |-------|----------|
  | `knowledge:agentic-architectural-patterns` | 6899 |
  | `knowledge:essential-graphrag` | 1103 |
  | `knowledge:graphrag-agentic` | 5900 |
  | `knowledge:ai-engineering-huyen` | 6526 |

  Re-measured 2026-10-07, after the Block C fix that makes `count_entities` — and the
  `bookgraph://catalog` resource — exclude soft-deleted (`merged_into`) entities. The earlier
  baseline (7111 / 1241 / 6078 / 6963, 2026-10-03) included **965 ghosts**, measured as the
  delta of this same smoke before and after the restart that loaded the fix
  (−212 / −138 / −178 / −437). A run that still shows the old numbers is running a binary
  from before that fix.

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

## 7. Client access (external MCP clients over Tailscale)

The endpoint is `http://<tailnet-host>:8003/sse` (plain HTTP *inside* the tailnet). Every path requires
`Authorization: Bearer <MCP_ACCESS_TOKEN>`: a client without the token, or with a wrong one, gets a `401` — which
means auth is working, not that the service is down.

The token lives in `<repo>/.env` (`MCP_ACCESS_TOKEN`, mode `600`, owner `gonzalo`). Hand it over out of band; never
paste it into a chat, an issue, or a commit.

Client configuration (Codex-style; any client needs the same two values):

```toml
[mcp_servers.bookgraph]
url = "http://gonpatri:8003/sse"
bearer_token_env_var = "BOOKGRAPH_MCP_TOKEN"
```

**The client reads that variable from its own process**, so set it at user level (Windows: `setx
BOOKGRAPH_MCP_TOKEN "..."`, or System Properties) and restart the client. A variable exported in one shell never
reaches a client launched from the menu, and the symptom is a `401`, not a connection error.

### Two different kinds of failure

| Symptom | Meaning | Where to fix |
|---------|---------|--------------|
| TCP refused or timeout, while `ping` works | the client cannot reach the port: a **tailnet policy (ACL)** restriction between different members, or a broken control path | the tailnet admin console, or Tailscale itself (below) — never the host firewall |
| `401 {"error":"unauthorized"}` | reachable and authenticated: the token is missing or wrong | the client's environment |
| connection refused from the host itself | nothing is listening | §2 and §3 |

### Do NOT bind 0.0.0.0

The service binds the Tailscale interface **on purpose** (R7, fail-closed); the configuration rejects a wildcard
bind in production. When a client cannot reach it, the fix is never "listen on 0.0.0.0": it is granting access in
the tailnet policy or exposing the service through Tailscale.

### Devices owned by another tailnet user

This host belongs to `gompatri@gmail.com`. A device owned by a **different member** of the same tailnet is
governed by the tailnet policy file, and it is normal for `ping` to work while `TCP 8003` does not. Grant it in the
admin console (Access controls, policy file):

```jsonc
{
  "acls": [
    // ... existing rules ...
    { "action": "accept", "src": ["user@example.com"], "dst": ["gonpatri:8003"] }
  ]
}
```

(`autogroup:member` instead of one user allows every member.) Then reconnect the client and retest.

**Plan B — expose it with TLS on 443**, a port tailnet policies almost always allow (needs `sudo` here):

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8003
# the client then uses: https://gonpatri.<tailnet>.ts.net/sse
```

### Diagnostic ladder (client side first)

```powershell
Test-NetConnection gonpatri -Port 8003   # the MCP port
Test-NetConnection gonpatri -Port 22     # another port: if 22 also fails it is user-level, not a port rule
tailscale status                         # does the client see this host online, and with which owner?
curl -i -m 6 http://gonpatri:8003/sse    # 401 = reachable and auth working; a timeout = port/ACL problem
```

```bash
# on this host: is the service up, and where is it listening?
systemctl is-active mcp-server; ss -ltnp | grep ':8003'; systemctl show mcp-server -p MainPID -p NRestarts
```

Known state to check when a **remote** client fails while a local one works: `tailscale status` here has reported
`Self.Online: false` with a stale control plane (relay only, no `curAddr`) while the data plane kept serving. A
peer on the same LAN is unaffected (direct path); a remote peer that depends on the relay can be flaky.
`sudo systemctl restart tailscaled` re-establishes it (it drops tailnet connectivity for a few seconds), and
`sudo tailscale set --operator=gonzalo` lets that user read `tailscale debug netmap` (the compiled packet filter)
without `sudo`.

### Parsing `.env`

Until 2026-10-04 the file carried leading spaces before the keys; they are gone now. If a script does `grep
'^MCP_ACCESS_TOKEN'` and gets nothing back — or a client sends an empty token and receives `401` — strip the
whitespace first: `sed -E 's/^[[:space:]]+//' .env`. Python-dotenv and `bash` `source` never needed that, but
anchored parsers do.

## 8. The read-only Neo4j credential (R2)

**First check the server edition; do not assume this credential is server-enforced.** The production
host currently runs Neo4j Community Edition 5.6. On Community, DBMS RBAC is unavailable: every
authenticated user has the same reach, and a distinct credential does not prevent a raw write. The
credential is nevertheless configured and used by the MCP for attribution and forward-compatibility.
The only write boundary on this edition is the application's managed read path (`READ_ACCESS` routing
plus `Neo4jQueryAdapter.execute_read`). In production the service **refuses to start** without the
separate credential, but that startup requirement does not create server-side privileges.

As an administrator, inspect the edition on the host:

    docker exec -it bookgraph-neo4j cypher-shell -d system

```cypher
CALL dbms.components() YIELD name, versions, edition
RETURN name, versions, edition;
```

### 8.1 Create the role (Enterprise/RBAC only, operator on the host)

Use this path only when the edition supports RBAC. Run as an administrator against the **system**
database:

    docker exec -it bookgraph-neo4j cypher-shell -d system

```cypher
CREATE ROLE bookgraph_reader;
GRANT ACCESS ON DATABASE neo4j TO bookgraph_reader;
GRANT MATCH {*} ON GRAPH neo4j NODE * TO bookgraph_reader;
GRANT MATCH {*} ON GRAPH neo4j RELATIONSHIP * TO bookgraph_reader;
DENY WRITE ON GRAPH neo4j TO bookgraph_reader;
GRANT SHOW INDEX ON DATABASE neo4j TO bookgraph_reader;
GRANT SHOW CONSTRAINT ON DATABASE neo4j TO bookgraph_reader;
GRANT EXECUTE PROCEDURE * ON DBMS TO bookgraph_reader;
GRANT EXECUTE BOOSTED PROCEDURE db.labels, db.relationshipTypes, db.propertyKeys ON DBMS TO bookgraph_reader;
CREATE USER bookgraph_reader SET PASSWORD '<a real password>' CHANGE NOT REQUIRED;
GRANT ROLE bookgraph_reader TO bookgraph_reader;
```

Why each grant matters — a missing one fails *one* tool, not everything, which is why the table doubles
as the diagnostic:

| grant | what needs it |
|-------|---------------|
| `ACCESS` + `MATCH {*}` on nodes and relationships | every read tool |
| `DENY WRITE` | belt and braces: nothing granted later can turn this role into a writer |
| `SHOW INDEX` / `SHOW CONSTRAINT` | a read path reporting the index state |
| `EXECUTE PROCEDURE *` | the fulltext search is a **procedure** (`db.index.fulltext.queryNodes`): without it `search_chunks` fails |
| `EXECUTE BOOSTED PROCEDURE db.labels, db.relationshipTypes, db.propertyKeys` | the Text2Cypher schema inference sees the whole schema rather than only what the role traverses |
| `EXECUTE BOOSTED PROCEDURE apoc.meta.data` | APOC schema introspection; verify whether this grant is required for the live role |
| `SHOW PROCEDURES` | Native `db.schema.*` availability probe; permission for this role is unverified on the live server |

`SHOW ROLE bookgraph_reader PRIVILEGES AS COMMANDS;` prints what was really granted — read it before
continuing. The APOC introspection call is `apoc.meta.data` (not `apoc.meta.schema`); verify whether
this procedure needs to be added to the boosted list for the live role. The native schema tier probes
`db.schema.*` availability with `SHOW PROCEDURES`. Whether `bookgraph_reader` may run that command is
still an open question, not a verified grant; check it against the live server before treating schema
inference as covered. On an RBAC-capable server, keep the migration order: create and verify the role,
set the environment, then deploy the code that requires the credential.

**Community Edition path:** do not run the role/grant statements above. `CREATE ROLE` and `GRANT ROLE`
are unsupported, and `SHOW USERS` reports `roles: NULL`; `DENY WRITE` therefore cannot be applied. If
a separate login does not already exist, create its identity on the **system** database (this does not
restrict its privileges on Community):

```cypher
CREATE USER bookgraph_reader SET PASSWORD '<a real password>' CHANGE NOT REQUIRED;
```

Then continue with §8.2 to configure the distinct credential. This remains useful for attribution and
forward-compatibility, but it is not read-only at the DBMS: a process using it can still issue a raw
write. On Community, the application's `READ_ACCESS` + managed `execute_read` path is the only write
boundary. Read §8.3 with that limitation in mind.

### 8.2 Point the service at it

In the host's `.env` (never print the password):

    NEO4J_READONLY_USER=bookgraph_reader
    NEO4J_READONLY_PASSWORD=<the same password>

Then `sudo systemctl restart mcp-server` (§2 and §3: **never** a manual instance).

The CLI read commands (`audit`, `gate`, `evaluate`, `query`, `validate`, and `schema verify`) use the
same credential when it is configured and fall back to the main one otherwise, so a run on a checkout
without these variables keeps working.

### 8.3 Verify, per tool

    cd ~/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi
    set -a; . ./.env; set +a
    ~/.local/bin/uv run --no-sync python scripts-ops/mcp_smoke_book4.py

The smoke test confirms that the service is reachable and its read path works; it does not prove
server-side write denial. The automated check runs the read assertion separately and checks whether a
raw write is denied with that credential:

    NEO4J_READONLY_TEST_URI=bolt://localhost:17687 \
    NEO4J_READONLY_USER=bookgraph_reader \
    NEO4J_READONLY_PASSWORD=<the read password> \
    uv run pytest tests/integration/test_readonly_neo4j_credential.py -q

On an RBAC-capable server, the write must be denied with `Neo.ClientError.Security.Forbidden`,
`Neo.ClientError.Statement.AccessMode` or `Neo.ClientError.General.ForbiddenOnReadOnlyDatabase` —
those are the three codes the test accepts. If a write succeeds, the test checks `SHOW USERS` for the
credential: non-NULL roles mean the server has RBAC and write acceptance is a failure; `roles: NULL`
identifies the Community path and skips only the server-denial assertion. The read test still runs on
Community, but its pass does **not** mean the server rejected a write; the application boundary
(`READ_ACCESS` + managed `execute_read`) remains the only one there. **Use those variable names**: the
test reads `NEO4J_READONLY_USER` and `NEO4J_READONLY_PASSWORD` (the same ones the service uses), so a
different name makes it skip silently instead of verifying anything. It skips unless the URI is set on
purpose, so it can never fall back to the application's configured server.

### 8.4 Rolling back

- **RBAC role and user (Enterprise only)**: `DROP USER bookgraph_reader; DROP ROLE bookgraph_reader;` —
  but once the code is deployed, production **requires** the credential, so this alone leaves the service
  unable to start.
- **Community user**: `DROP USER bookgraph_reader;` — there is no role to drop on Community.
- **The feature**: remove the two variables from `.env`, deploy the previous commit, restart. Neither
  path touches the graph: the role is security configuration, not graph data.

## 9. Related rules

- Graph mutation gate (AGENTS.md §7.2): fresh backup → dry-run → human approval
  → apply → audit. This runbook is read-only with respect to the graph.
- AGENTS.md §7: this host runs 23 production containers from unrelated
  projects. Touch only `bookgraph-neo4j`; never prune, remove, or restart
  anything else.
