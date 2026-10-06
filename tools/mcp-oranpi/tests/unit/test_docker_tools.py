"""Tests for DockerTools in application/docker_tools.py.

Uses MockCommandRunner to test docker tools without SSH.
"""

from __future__ import annotations

import pytest

from mcp_oranpi.application.docker_tools import DockerTools
from mcp_oranpi.domain.errors import (
    CONN_FAILED,
    CONN_TIMEOUT,
    DOCKER_NOT_FOUND,
    DOCKER_NOT_RUNNING,
    DOCKER_UNAVAILABLE,
)
from mcp_oranpi.infrastructure.ssh_client import CommandResult
from tests.helpers import MockCommandRunner


@pytest.fixture
def mock_runner() -> MockCommandRunner:
    """Provide a MockCommandRunner for DockerTools tests."""
    return MockCommandRunner()


@pytest.fixture
def docker_tools(mock_runner: MockCommandRunner) -> DockerTools:
    """Provide a DockerTools instance with a mock runner."""
    return DockerTools(mock_runner)


class TestDockerListContainers:
    """Tests for docker_list_containers."""

    async def test_success(self, docker_tools: DockerTools) -> None:
        stdout = """{"ID":"abc123","Names":"nginx","Image":"nginx:latest","Status":"Up 2 hours","CreatedAt":"","Ports":"80/tcp->0.0.0.0:8080"}
{"ID":"def456","Names":"redis","Image":"redis:alpine","Status":"Up 5 days","CreatedAt":"","Ports":"6379/tcp->0.0.0.0:6379"}"""
        docker_tools._runner.set_response(  # type: ignore
            "docker_ps",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await docker_tools.docker_list_containers()

        assert result.error is None
        assert result.data is not None
        assert result.data["total_count"] == 2
        assert result.data["returned_count"] == 2
        assert len(result.data["containers"]) == 2  # type: ignore

    async def test_with_all_flag(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_ps_all",
            CommandResult(exit_code=0, stdout="", stderr=""),
        )

        result = await docker_tools.docker_list_containers(all=True)

        assert result.error is None
        last_call = docker_tools._runner.last_call()  # type: ignore
        assert last_call is not None
        assert last_call[0] == "docker_ps_all"

    async def test_with_filter(self, docker_tools: DockerTools) -> None:
        stdout = """{"ID":"abc123","Names":"nginx-proxy","Image":"nginx","Status":"Up","CreatedAt":"","Ports":""}
{"ID":"def456","Names":"nginx-web","Image":"nginx","Status":"Up","CreatedAt":"","Ports":""}"""
        docker_tools._runner.set_response(  # type: ignore
            "docker_ps",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await docker_tools.docker_list_containers(filter="web")

        assert result.error is None
        assert result.data is not None
        assert len(result.data["containers"]) == 1  # type: ignore
        assert result.data["containers"][0]["name"] == "nginx-web"  # type: ignore

    async def test_docker_unavailable(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_ps",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="cannot connect to the docker daemon",
            ),
        )

        result = await docker_tools.docker_list_containers()

        assert result.error is not None
        assert result.error.code == DOCKER_UNAVAILABLE

    async def test_timeout(self, docker_tools: DockerTools, mock_runner: MockCommandRunner) -> None:
        mock_runner.set_response(
            "docker_ps",
            CommandResult(exit_code=0, stdout="", stderr=""),
        )

        # Simulate timeout by making run raise TimeoutError
        async def timeout_run(*args: object, **kwargs: object) -> CommandResult:
            raise TimeoutError()

        mock_runner.run = timeout_run  # type: ignore

        result = await docker_tools.docker_list_containers()

        assert result.error is not None
        assert result.error.code == CONN_TIMEOUT

    async def test_connection_error(
        self, docker_tools: DockerTools, mock_runner: MockCommandRunner
    ) -> None:
        async def conn_error_run(*args: object, **kwargs: object) -> CommandResult:
            raise ConnectionError()

        mock_runner.run = conn_error_run  # type: ignore

        result = await docker_tools.docker_list_containers()

        assert result.error is not None
        assert result.error.code == CONN_FAILED


class TestDockerInspectContainer:
    """Tests for docker_inspect_container."""

    async def test_success(self, docker_tools: DockerTools) -> None:
        stdout = """[{
  "Id": "abc123def456",
  "Name": "/nginx",
  "Created": "2024-01-15T10:30:00Z",
  "Config": {"Image": "nginx:latest", "Env": ["NGINX_PORT=80"], "Labels": {}},
  "State": {"Status": "running"},
  "NetworkSettings": {"Networks": {}, "Ports": {}},
  "Mounts": [],
  "HostConfig": {"RestartPolicy": {"Name": "always"}}
}]"""
        docker_tools._runner.set_response(  # type: ignore
            "docker_inspect",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await docker_tools.docker_inspect_container("nginx")

        assert result.error is None
        assert result.data is not None
        assert result.data["name"] == "nginx"
        assert result.data["image"] == "nginx:latest"
        assert result.data["restart_policy"] == "always"

    async def test_not_found(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_inspect",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="Error: No such object: nonexistent",
            ),
        )

        result = await docker_tools.docker_inspect_container("nonexistent")

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_FOUND

    async def test_invalid_container_name(self, docker_tools: DockerTools) -> None:
        result = await docker_tools.docker_inspect_container("; rm -rf /")

        assert result.error is not None
        assert result.error.code == "VALID_PARAM_INVALID"

    async def test_parse_failure(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_inspect",
            CommandResult(exit_code=0, stdout="not json", stderr=""),
        )

        result = await docker_tools.docker_inspect_container("nginx")

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_FOUND


class TestDockerContainerLogs:
    """Tests for docker_container_logs."""

    async def test_success(self, docker_tools: DockerTools) -> None:
        stdout = "2024-01-15 10:30:00 App started\n2024-01-15 10:30:01 Request received"
        docker_tools._runner.set_response(  # type: ignore
            "docker_logs",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await docker_tools.docker_container_logs("nginx", tail=100)

        assert result.error is None
        assert result.data is not None
        assert result.data["container"] == "nginx"
        assert result.data["log_count"] == 2

    async def test_with_since_param(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_logs",
            CommandResult(exit_code=0, stdout="recent logs", stderr=""),
        )

        result = await docker_tools.docker_container_logs("nginx", since="1h")

        assert result.error is None
        last_call = docker_tools._runner.last_call()  # type: ignore
        assert last_call is not None
        assert "since" in last_call[1]

    async def test_not_found(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_logs",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="Error: No such container: nonexistent",
            ),
        )

        result = await docker_tools.docker_container_logs("nonexistent")

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_FOUND

    async def test_not_running(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_logs",
            CommandResult(exit_code=1, stdout="", stderr="container not running"),
        )

        result = await docker_tools.docker_container_logs("stopped_container")

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_RUNNING


class TestDockerContainerStats:
    """Tests for docker_container_stats."""

    async def test_success(self, docker_tools: DockerTools) -> None:
        stdout = '{"Container":"nginx","CPUPerc":"15.5%","MemUsage":"256MiB / 512MiB","MemPerc":"50.0%","NetIO":"1.2kB / 3.4kB","BlockIO":"5.6MB / 7.8MB","Pids":4}'
        docker_tools._runner.set_response(  # type: ignore
            "docker_stats",
            CommandResult(exit_code=0, stdout=stdout, stderr=""),
        )

        result = await docker_tools.docker_container_stats("nginx")

        assert result.error is None
        assert result.data is not None
        assert result.data["cpu_percent"] == 15.5
        assert result.data["memory_usage_mb"] == 256.0

    async def test_container_not_running(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_stats",
            CommandResult(
                exit_code=1,
                stdout="",
                stderr="Error: No such container: stopped",
            ),
        )

        result = await docker_tools.docker_container_stats("stopped")

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_FOUND

    async def test_parse_failure(self, docker_tools: DockerTools) -> None:
        docker_tools._runner.set_response(  # type: ignore
            "docker_stats",
            CommandResult(exit_code=0, stdout="not json", stderr=""),
        )

        result = await docker_tools.docker_container_stats("nginx")

        assert result.error is not None
        assert result.error.code == DOCKER_NOT_RUNNING


class TestDockerInspectPorts:
    """Tests for docker_inspect_ports."""

    async def test_success_all_ports(self, docker_tools: DockerTools) -> None:
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1234,fd=6))
tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=1235,fd=7))"""
        docker_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )

        result = await docker_tools.docker_inspect_ports()

        assert result.error is None
        assert result.data is not None
        assert len(result.data["occupied_ports"]) == 2  # type: ignore

    async def test_with_container_filter(self, docker_tools: DockerTools) -> None:
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("docker-proxy",pid=1000,fd=6))"""
        inspect_output = """[{
  "Id": "abc123",
  "Name": "/nginx",
  "Created": "2024-01-15T10:30:00Z",
  "Config": {"Image": "nginx", "Env": [], "Labels": {}},
  "State": {"Status": "running"},
  "NetworkSettings": {
    "Networks": {},
    "Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}
  },
  "Mounts": [],
  "HostConfig": {"RestartPolicy": {}}
}]"""
        docker_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )
        docker_tools._runner.set_response(  # type: ignore
            "docker_inspect",
            CommandResult(exit_code=0, stdout=inspect_output, stderr=""),
        )

        result = await docker_tools.docker_inspect_ports(container="nginx")

        assert result.error is None
        assert result.data is not None
        assert result.data["container"] == "nginx"

    async def test_tcp_protocol_filter(self, docker_tools: DockerTools) -> None:
        ss_output = """Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port  Process
tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=1234,fd=6))
udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("dnsmasq",pid=567,fd=5))"""
        docker_tools._runner.set_response(  # type: ignore
            "ss_tulnp",
            CommandResult(exit_code=0, stdout=ss_output, stderr=""),
        )

        result = await docker_tools.docker_inspect_ports(protocol="tcp")

        assert result.error is None
        assert result.data is not None
        # Only TCP should be returned
        assert all(p["protocol"] == "tcp" for p in result.data["occupied_ports"])  # type: ignore
