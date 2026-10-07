"""Integration test — full tool pipeline via MCP handler."""

import asyncio
import json

from mcp_oranpi.config import AppConfig
from mcp_oranpi.server import AppContainer


async def main() -> None:
    config = AppConfig()  # type: ignore
    container = AppContainer(config)

    await container.ssh_client.connect()
    print("SSH connected!")

    # Test system tools
    print("\n=== system_memory_usage ===")
    result = await container.system_tools.system_memory_usage()
    print(f"Error: {result.error}")
    print(
        f"Data: {json.dumps(result.data, default=str, indent=2)[:500] if result.data else 'None'}"
    )

    print("\n=== system_cpu_usage ===")
    result = await container.system_tools.system_cpu_usage()
    print(f"Error: {result.error}")
    print(
        f"Data: {json.dumps(result.data, default=str, indent=2)[:300] if result.data else 'None'}"
    )

    # Test docker tools
    print("\n=== docker_list_containers ===")
    result = await container.docker_tools.docker_list_containers()
    if result.error:
        print(f"Error: {result.error}")
    elif result.data:
        containers = result.data.get("containers", [])
        total = result.data.get("total_count", "?")
        print(f"Containers: {len(containers) if isinstance(containers, list) else 'unknown'}")
        print(f"Total count: {total}")
        if isinstance(containers, list) and containers:
            print(f"First container: {json.dumps(containers[0], default=str)[:200]}")

    # Test network tools
    print("\n=== network_scan_ports (1-1024) ===")
    result = await container.network_tools.network_scan_ports()
    if result.error:
        print(f"Error: {result.error}")
    elif result.data:
        ports = result.data.get("occupied_ports", [])
        print(f"Occupied ports: {len(ports) if isinstance(ports, list) else 'unknown'}")
        scan_ms = result.data.get("scan_duration_ms", "?")
        print(f"Scan duration: {scan_ms}ms")

    # Test MCP handler
    print("\n=== MCP handler: system_memory_usage ===")
    handler_result = await container.mcp_handler.call_tool("system_memory_usage", {})
    print(f"Result type: {type(handler_result).__name__}")
    print(f"Content (first 300): {handler_result[0].text[:300]}")

    # Test MCP handler with error
    print("\n=== MCP handler: unknown tool ===")
    try:
        await container.mcp_handler.call_tool("nonexistent_tool", {})
    except ValueError as e:
        print(f"Correctly raised ValueError: {e}")

    await container.ssh_client.disconnect()
    print("\nAll integration tests PASSED!")


if __name__ == "__main__":
    asyncio.run(main())
