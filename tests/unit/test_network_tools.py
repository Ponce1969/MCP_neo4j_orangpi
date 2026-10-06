"""Tests for NetworkTools in application/network_tools.py.

Uses MockCommandRunner to test network tools without SSH.
"""

from __future__ import annotations

import pytest

from mcp_oranpi.application.network_tools import NetworkTools
from mcp_oranpi.domain.errors import (
    NET_SCAN_RANGE_INVALID,
    NET_SCAN_TIMEOUT,
    NET_TAILSCALE_NOT_INSTALLED,
)
from mcp_oranpi.infrastructure.ssh_client import CommandResult
from tests.helpers import MockCommandRunner


@pytest.fixture
def mock_runner() -> MockCommandRunner:
    """Provide a MockCommandRunner for NetworkTools tests."""
    return MockCommandRunner()


@pytest.fixture
def network_tools(mock_runner: MockCommandRunner) -> NetworkTools:
    """Provide a NetworkTools instance with a mock runner."""
    return NetworkTools(mock_runner)


class TestNetworkScanPorts:
    """Tests for network_scan_ports."""

    async def test_success(self, network_tools: NetworkTools) -> None:
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1234,fd=6))
tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=1235,fd=7))
tcp LISTEN 0 128 127.0.0.1:3000 0.0.0.0:* users:(("node",pid=2000,fd=6))"""
        network_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )
        network_tools._runner.set_response(  # type: ignore
            "hostname",
            CommandResult(exit_code=0, stdout="oranpi.local", stderr=""),
        )

        result = await network_tools.network_scan_ports(range_start=1, range_end=9000)

        assert result.error is None
        assert result.data is not None
        assert len(result.data["occupied_ports"]) == 3  # type: ignore
        assert result.data["host"] == "oranpi.local"
        # The host IP is part of the OS socket truth, not an assumed wildcard.
        addresses = [p["local_address"] for p in result.data["occupied_ports"]]  # type: ignore
        assert "127.0.0.1:3000" in addresses
        assert "0.0.0.0:8080" in addresses

    async def test_range_filter(self, network_tools: NetworkTools) -> None:
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=1,fd=6))
tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=2,fd=6))
tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=3,fd=6))"""
        network_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )
        network_tools._runner.set_response(  # type: ignore
            "hostname",
            CommandResult(exit_code=0, stdout="test", stderr=""),
        )

        result = await network_tools.network_scan_ports(range_start=80, range_end=443)

        assert result.error is None
        assert result.data is not None
        assert len(result.data["occupied_ports"]) == 2  # type: ignore

    async def test_invalid_range_start_greater_than_end(self, network_tools: NetworkTools) -> None:
        result = await network_tools.network_scan_ports(range_start=9000, range_end=1000)

        assert result.error is not None
        assert result.error.code == NET_SCAN_RANGE_INVALID

    async def test_invalid_port_out_of_range(self, network_tools: NetworkTools) -> None:
        result = await network_tools.network_scan_ports(range_start=0, range_end=1024)

        assert result.error is not None
        assert result.error.code == NET_SCAN_RANGE_INVALID

    async def test_range_too_large(self, network_tools: NetworkTools) -> None:
        result = await network_tools.network_scan_ports(range_start=1, range_end=20000)

        assert result.error is not None
        assert result.error.code == NET_SCAN_RANGE_INVALID

    async def test_timeout(
        self, network_tools: NetworkTools, mock_runner: MockCommandRunner
    ) -> None:
        async def timeout_run(*args: object, **kwargs: object) -> CommandResult:
            raise TimeoutError()

        mock_runner.run = timeout_run  # type: ignore

        result = await network_tools.network_scan_ports()

        assert result.error is not None
        assert result.error.code == NET_SCAN_TIMEOUT


class TestNetworkSuggestPort:
    """Tests for network_suggest_port."""

    async def test_preferred_port_available(self, network_tools: NetworkTools) -> None:
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1234,fd=6))"""
        network_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )

        result = await network_tools.network_suggest_port(preferred_port=8080)

        assert result.error is None
        assert result.data is not None
        assert result.data["preferred_port"] == 8080
        assert result.data["preferred_available"] is False

    async def test_preferred_port_occupied(self, network_tools: NetworkTools) -> None:
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:9000 0.0.0.0:* users:(("app",pid=1,fd=6))"""
        network_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )

        result = await network_tools.network_suggest_port(preferred_port=8080)

        assert result.error is None
        assert result.data is not None
        assert result.data["preferred_available"] is True
        assert len(result.data["suggestions"]) > 0  # type: ignore

    async def test_invalid_port(self, network_tools: NetworkTools) -> None:
        result = await network_tools.network_suggest_port(preferred_port=0)

        assert result.error is not None
        assert result.error.code == "VALID_PARAM_INVALID"


class TestNetworkInspectBindings:
    """Tests for network_inspect_bindings."""

    async def test_success(self, network_tools: NetworkTools) -> None:
        # Note: parse_ss_tulnp only returns LISTEN state ports
        # UDP UNCONN ports are filtered out
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1234,fd=6))
tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=1235,fd=7))
udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("dnsmasq",pid=567,fd=5))"""
        network_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )

        result = await network_tools.network_inspect_bindings()

        assert result.error is None
        assert result.data is not None
        # Only TCP LISTEN ports are returned (2), UDP UNCONN is filtered
        assert len(result.data["bindings"]) == 2  # type: ignore

    async def test_reports_the_real_os_socket_address(self, network_tools: NetworkTools) -> None:
        # Regression: a Tailscale-bound Uvicorn listener was reported as 0.0.0.0.
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp   LISTEN 0      2048   100.106.85.109:8003    0.0.0.0:*    users:(("book-graph-rag-",pid=3085526,fd=6))
	tcp   LISTEN 0      4096   [::]:7474              [::]:*       users:(("docker-proxy",pid=1234,fd=8))"""
        network_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )

        result = await network_tools.network_inspect_bindings()

        assert result.error is None
        assert result.data is not None
        bindings: list[dict[str, object]] = result.data["bindings"]  # type: ignore[assignment]
        assert bindings[0]["local_address"] == "100.106.85.109:8003"
        assert bindings[0]["pid"] == 3085526
        assert bindings[1]["local_address"] == "[::]:7474"

    async def test_empty(self, network_tools: NetworkTools) -> None:
        network_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout="", stderr=""),
        )

        result = await network_tools.network_inspect_bindings()

        assert result.error is None
        assert result.data is not None
        assert len(result.data["bindings"]) == 0  # type: ignore


class TestNetworkTailscaleStatus:
    """Tests for network_tailscale_status."""

    async def test_online(self, network_tools: NetworkTools) -> None:
        stdout = """{
  "Self": {
    "Online": true,
    "TailscaleIPs": ["100.64.1.1"],
    "DNSName": "oranpi.tailscale.local"
  },
  "Peers": {
    "peer1": {"HostName": "macbook", "TailscaleIPs": ["100.64.1.2"], "Online": true}
  }
}"""
        network_tools._runner.set_response(  # type: ignore
            "tailscale_status",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await network_tools.network_tailscale_status()

        assert result.error is None
        assert result.data is not None
        assert result.data["online"] is True
        assert result.data["dns_name"] == "oranpi.tailscale.local"
        assert len(result.data["peers"]) == 1  # type: ignore

    async def test_offline(self, network_tools: NetworkTools) -> None:
        stdout = '{"Self": {"Online": false}}'
        network_tools._runner.set_response(  # type: ignore
            "tailscale_status",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await network_tools.network_tailscale_status()

        assert result.error is None
        assert result.data is not None
        assert result.data["online"] is False

    async def test_not_installed(self, network_tools: NetworkTools) -> None:
        network_tools._runner.set_response(  # type: ignore
            "tailscale_status",
            CommandResult(
                exit_code=127,
                stdout="",
                stderr="tailscale: command not found",
            ),
        )

        result = await network_tools.network_tailscale_status()

        assert result.error is not None
        assert result.error.code == NET_TAILSCALE_NOT_INSTALLED

    async def test_error_exit_code(self, network_tools: NetworkTools) -> None:
        network_tools._runner.set_response(  # type: ignore
            "tailscale_status",
            CommandResult(exit_code=1, stdout="", stderr="some error"),
        )

        result = await network_tools.network_tailscale_status()

        assert result.error is not None
        assert result.error.code == NET_TAILSCALE_NOT_INSTALLED
