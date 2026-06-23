# MCP Server Deployment

This folder contains the systemd unit file for running the Book Graph RAG MCP server on the Orange Pi.

## Files

- `mcp-server.service` — systemd unit that starts `book-graph-rag-mcp serve` on boot.

## Prerequisites

- The repo is cloned at `/home/bookgraph/Gonzalo_codigo/Mcp_libro/MCP_neo4j_orangpi`.
- `uv` is installed for the `bookgraph` user.
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

   Update `User`, `Group`, `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` if needed.

3. Reload systemd and enable the service:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable mcp-server
   ```

4. Start the service:

   ```bash
   sudo systemctl start mcp-server
   ```

5. Check the status:

   ```bash
   sudo systemctl status mcp-server
   ```

6. Follow the logs:

   ```bash
   journalctl -u mcp-server -f
   ```

## Verification

From the Pi or any Tailscale-connected peer:

```bash
curl http://localhost:8003/sse
```

You should see an SSE stream response.

## Rollback

To stop and disable the service:

```bash
sudo systemctl stop mcp-server
sudo systemctl disable mcp-server
```
