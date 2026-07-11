"""Integration test — verify secret redaction against real OrangePi."""
import asyncio
import json

from mcp_oranpi.config import AppConfig
from mcp_oranpi.server import AppContainer


async def main() -> None:
    config = AppConfig()
    container = AppContainer(config)
    await container.ssh_client.connect()

    # Test 1: docker_inspect should REDACT passwords and secrets
    print("=== TEST 1: docker_inspect redaction ===")
    result = await container.mcp_handler.call_tool("docker_inspect_container", {"container": "svl-app"})
    data = json.loads(result[0].text)
    env_list = data.get("data", {}).get("env", [])
    for env in env_list[:8]:
        print(f"  {env}")

    # Verify PASSWORD and SECRET are redacted
    secret_found = False
    for env in env_list:
        if "REDACTED" in env:
            print(f"  [OK] Redacted: {env.split('=')[0]}")
        if any(s in env for s in ["5iycuxnRUzZ1bmB8GRrr1MHD", "drj6DLOrSB4d0HqE0Rj82DmNS4vfJYgL"]):
            secret_found = True
    if secret_found:
        print("  [FAIL] Secret exposed in env vars!")
    else:
        print("  [OK] No secrets found in env vars")

    # Test 2: logs_file should BLOCK .cloudflared paths
    print("\n=== TEST 2: logs_file blocks .cloudflared ===")
    result = await container.mcp_handler.call_tool("logs_file", {"path": "/home/gonzalo/.cloudflared/config.yml"})
    data = json.loads(result[0].text)
    code = data.get("error", {}).get("code", "")
    msg = data.get("error", {}).get("message", "")[:80]
    print(f"  error.code: {code}")
    print(f"  error.message: {msg}")
    if code == "LOG_PATH_FORBIDDEN":
        print("  [OK] Cloudflared path correctly blocked")
    else:
        print("  [FAIL] Cloudflared path was NOT blocked!")

    # Test 3: logs_file should BLOCK .pem files
    print("\n=== TEST 3: logs_file blocks .pem files ===")
    result = await container.mcp_handler.call_tool("logs_file", {"path": "/etc/ssl/certs/server.pem"})
    data = json.loads(result[0].text)
    code = data.get("error", {}).get("code", "")
    print(f"  error.code: {code}")
    if code == "LOG_PATH_FORBIDDEN":
        print("  [OK] .pem file correctly blocked")
    else:
        print("  [FAIL] .pem file was NOT blocked!")

    # Test 4: logs_file should BLOCK .ssh paths
    print("\n=== TEST 4: logs_file blocks .ssh ===")
    result = await container.mcp_handler.call_tool("logs_file", {"path": "/home/gonzalo/.ssh/id_rsa"})
    data = json.loads(result[0].text)
    code = data.get("error", {}).get("code", "")
    print(f"  error.code: {code}")
    if code == "LOG_PATH_FORBIDDEN":
        print("  [OK] .ssh path correctly blocked")
    else:
        print("  [FAIL] .ssh path was NOT blocked!")

    # Test 5: logs_file should ALLOW normal logs
    print("\n=== TEST 5: logs_file allows /var/log/syslog ===")
    result = await container.mcp_handler.call_tool("logs_file", {"path": "/var/log/syslog", "tail": 3})
    data = json.loads(result[0].text)
    has_data = "data" in data and "error" not in data
    print(f"  [OK] Allowed: {has_data}")

    # Test 6: system_temperatures should work
    print("\n=== TEST 6: system_temperatures ===")
    result = await container.mcp_handler.call_tool("system_temperatures", {})
    data = json.loads(result[0].text)
    source = data.get("data", {}).get("source", "unknown")
    temps = data.get("data", {}).get("temperatures", [])
    print(f"  source: {source}")
    for t in temps[:3]:
        print(f"  {t['name']}: {t['temp_c']}C")

    await container.ssh_client.disconnect()
    print("\n=== ALL SECURITY TESTS COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())