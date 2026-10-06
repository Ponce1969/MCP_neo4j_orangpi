# MCP Server Deployment

This folder contains the systemd unit file for running the Book Graph RAG MCP server on the Orange Pi.

## Files

- `mcp-server.service` — systemd unit that starts `book-graph-rag-mcp serve` on boot.
- `secure_gatekeeper.sh` — host-side command whitelist for the agent key (see below).

## Network binding (R7)

The MCP SSE server must only ever listen on a **private/Tailscale interface**, never
on a public address. The service unit sets `Environment=MCP_BIND_HOST=100.106.85.109`
(the Orange Pi's Tailscale IP), which overrides any `MCP_BIND_HOST` in the `.env`
EnvironmentFile and takes precedence in systemd. The application also fail-fast rejects
a wildcard bind (`0.0.0.0`, `::`, or empty) whenever `APP_ENV=production`, so the server
can never accidentally expose the MCP boundary to the public network.

When the Tailscale IP changes, update the `Environment=MCP_BIND_HOST=...` line in
`deploy/mcp-server.service` and re-run `sudo systemctl daemon-reload` <!-- no-live-deployment-allow -->.

## SSH agent gatekeeper (host-side)

The audit MCP reaches this host through a forced command: the host's
`authorized_keys` wraps the agent key with a gatekeeper script that whitelists
which commands may run. The canonical copy lives in this repository at
`deploy/secure_gatekeeper.sh`.

**A `git pull` does not install it.** After changing the script, install the
canonical copy on the host:

```bash
install -m 700 deploy/secure_gatekeeper.sh ~/scripts/secure_gatekeeper.sh
bash -n ~/scripts/secure_gatekeeper.sh   # syntax check
```

Rules:

- Keep LF line endings. A CRLF copy fails to parse and rejects every agent-key
  connection until it is restored. Verify with
  `python3 -c "print(open('secure_gatekeeper.sh','rb').read().count(b'\r'))"`;
  it must print `0`.
- Back up the installed revision before overwriting it
  (`secure_gatekeeper.sh.bak-<timestamp>`).
- No service restart is needed: the forced command runs the script on each new
  connection, so a change takes effect immediately.
- Known debt: the argument guard is a character denylist. It must move to a
  strict allowlist, and the reachable `journalctl`/`docker logs` options must be
  narrowed.

## Prerequisites

- The repo is cloned at `/home/gonzalo/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi`.
- `uv` is installed for the `gonzalo` user at `/home/gonzalo/.local/bin/uv`.
- A `.env` file exists in the repo root with all required Neo4j and MCP variables.

## Installation

1. Copy the service file into place:

   ```bash
   sudo cp deploy/mcp-server.service /etc/systemd/system/mcp-server.service
   ```

2. Edit the file to match the actual Pi user and paths:

   ```bash
   sudo nano /etc/systemd/system/mcp-server.service
   ```

   The service file is preconfigured for user `gonzalo` on the Orange Pi. Update if your setup differs.

3. Reload systemd and enable the service:

   ```bash
   sudo systemctl daemon-reload  # no-live-deployment-allow
   sudo systemctl enable mcp-server  # no-live-deployment-allow
   ```

4. Start the service:

   ```bash
   sudo systemctl start mcp-server  # no-live-deployment-allow
   ```

5. Check the status:

   ```bash
   sudo systemctl status mcp-server  # no-live-deployment-allow
   ```

6. Follow the logs:

   ```bash
   journalctl -u mcp-server -f
   ```

## Verification

From the Pi or any Tailscale-connected peer:

```bash
curl http://100.106.85.109:8003/sse  # no-live-deployment-allow
```

You should see an SSE stream response.

## Rollback

To stop and disable the service:

```bash
sudo systemctl stop mcp-server  # no-live-deployment-allow
sudo systemctl disable mcp-server  # no-live-deployment-allow
```
