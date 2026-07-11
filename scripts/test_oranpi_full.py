"""Comprehensive integration test — all 22 tools against real OrangePi."""
import asyncio
import json

from mcp_oranpi.config import AppConfig
from mcp_oranpi.server import AppContainer


async def main() -> None:
    config = AppConfig()
    container = AppContainer(config)

    await container.ssh_client.connect()
    print("SSH connected!")

    # Load workspace registry
    ws = await container.workspace_resolver.load()
    print(f"Workspaces: {len(ws)}")
    for ws_id, ws_info in ws.items():
        compose = "compose" if ws_info.has_compose else "no-compose"
        git = "git" if ws_info.has_git else "no-git"
        print(f"  {ws_id}: {ws_info.status} - {ws_info.path} [{compose}, {git}]")

    h = container.mcp_handler
    passed = 0
    failed = 0

    async def test_tool(name: str, args: dict) -> None:
        nonlocal passed, failed
        try:
            result = await container.mcp_handler.call_tool(name, args)
            data = json.loads(result[0].text)
            if "error" in data:
                code = data["error"]["code"]
                msg = data["error"]["message"]
                print(f"  {name}: ERROR - {code}: {msg}")
                failed += 1
            else:
                text = result[0].text[:120]
                print(f"  {name}: OK ({len(result[0].text)} bytes) - {text}...")
                passed += 1
        except Exception as e:
            print(f"  {name}: EXCEPTION - {type(e).__name__}: {e}")
            failed += 1

    # ── Docker Tools (5) ──────────────────────────────────────────────────
    print("\n=== Docker Tools ===")
    await test_tool("docker_list_containers", {})
    await test_tool("docker_list_containers", {"all": True})
    await test_tool("docker_inspect_container", {"container": "svl-app"})
    await test_tool("docker_container_logs", {"container": "svl-app", "tail": 5})
    await test_tool("docker_container_stats", {"container": "svl-app"})

    # ── Network Tools (4) ─────────────────────────────────────────────────
    print("\n=== Network Tools ===")
    await test_tool("network_scan_ports", {"range_start": 1, "range_end": 1024})
    await test_tool("network_suggest_port", {"preferred_port": 8080, "range": 50, "count": 3})
    await test_tool("network_inspect_bindings", {})
    await test_tool("network_tailscale_status", {})

    # ── System Tools (5) ──────────────────────────────────────────────────
    print("\n=== System Tools ===")
    await test_tool("system_cpu_usage", {})
    await test_tool("system_memory_usage", {})
    await test_tool("system_disk_usage", {})
    await test_tool("system_temperatures", {})
    await test_tool("system_service_status", {"service": "docker"})

    # ── Logs Tools (3) ────────────────────────────────────────────────────
    print("\n=== Logs Tools ===")
    await test_tool("logs_docker", {"container": "svl-app", "tail": 5})
    await test_tool("logs_systemd", {"unit": "docker", "tail": 5})
    await test_tool("logs_file", {"path": "/var/log/syslog", "tail": 5})

    # ── Workspace Tools (5) ───────────────────────────────────────────────
    print("\n=== Workspace Tools ===")
    await test_tool("workspace_list", {})
    await test_tool("workspace_inspect", {"workspace": "meli_bunker"})

    meli_ws = ws.get("meli_bunker")
    if meli_ws and meli_ws.has_compose:
        await test_tool("workspace_docker_ps", {"workspace": "meli_bunker"})
        await test_tool("workspace_logs", {"workspace": "meli_bunker", "tail": 5})
        await test_tool("workspace_ports", {"workspace": "meli_bunker"})
    else:
        print("  Skipping workspace docker tools (meli_bunker has no compose detected)")

    await container.ssh_client.disconnect()
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tools")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())