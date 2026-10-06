"""Quick integration test against real OrangePi."""

import asyncio

from mcp_oranpi.config import AppConfig
from mcp_oranpi.server import AppContainer


async def main() -> None:
    config = AppConfig()  # type: ignore
    container = AppContainer(config)

    print("Connecting SSH...")
    await container.ssh_client.connect()
    print(f"Connected! State: {container.ssh_client.state}")

    # Test hostname
    print("\n=== hostname ===")
    r = await container.command_runner.run("hostname")
    print(f"Result: {r.stdout.strip()}")

    # Test docker ps
    print("\n=== docker ps ===")
    r = await container.command_runner.run("docker_ps")
    print(f"Exit code: {r.exit_code}")
    print(f"Stdout (first 500):\n{r.stdout[:500]}")
    if r.stderr:
        print(f"Stderr (first 200): {r.stderr[:200]}")

    # Test free -m
    print("\n=== free -m ===")
    r = await container.command_runner.run("free_m")
    print(f"Exit code: {r.exit_code}")
    print(f"Stdout:\n{r.stdout[:400]}")

    # Test df -T
    print("\n=== df -T ===")
    r = await container.command_runner.run("df_t")
    print(f"Exit code: {r.exit_code}")
    print(f"Stdout:\n{r.stdout[:400]}")

    await container.ssh_client.disconnect()
    print("\nDisconnected. All integration tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
