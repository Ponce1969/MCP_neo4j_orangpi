"""Tests for MCPHandler and TOOL_DEFINITIONS in presentation/mcp_handlers.py.

Tests the MCP protocol handler in isolation using mocked tool instances.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from mcp_oranpi.domain.errors import ToolError, ToolResult
from mcp_oranpi.presentation.mcp_handlers import TOOL_DEFINITIONS, MCPHandler

# ── Tool Name Constants ─────────────────────────────────────────────────────────

DOCKER_TOOL_NAMES = [
    "docker_list_containers",
    "docker_inspect_container",
    "docker_container_logs",
    "docker_container_stats",
    "docker_inspect_ports",
]

NETWORK_TOOL_NAMES = [
    "network_scan_ports",
    "network_suggest_port",
    "network_inspect_bindings",
    "network_tailscale_status",
]

SYSTEM_TOOL_NAMES = [
    "system_cpu_usage",
    "system_memory_usage",
    "system_disk_usage",
    "system_temperatures",
    "system_service_status",
]

LOGS_TOOL_NAMES = [
    "logs_docker",
    "logs_systemd",
    "logs_file",
]

WORKSPACE_TOOL_NAMES = [
    "workspace_list",
    "workspace_inspect",
    "workspace_docker_ps",
    "workspace_logs",
    "workspace_ports",
]

ALL_TOOL_NAMES = (
    DOCKER_TOOL_NAMES
    + NETWORK_TOOL_NAMES
    + SYSTEM_TOOL_NAMES
    + LOGS_TOOL_NAMES
    + WORKSPACE_TOOL_NAMES
)


# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_docker_tools() -> mock.AsyncMock:
    """Provide an AsyncMock for DockerTools."""
    return mock.AsyncMock()


@pytest.fixture
def mock_network_tools() -> mock.AsyncMock:
    """Provide an AsyncMock for NetworkTools."""
    return mock.AsyncMock()


@pytest.fixture
def mock_system_tools() -> mock.AsyncMock:
    """Provide an AsyncMock for SystemTools."""
    return mock.AsyncMock()


@pytest.fixture
def mock_logs_tools() -> mock.AsyncMock:
    """Provide an AsyncMock for LogsTools."""
    return mock.AsyncMock()


@pytest.fixture
def mock_workspace_tools() -> mock.AsyncMock:
    """Provide an AsyncMock for WorkspaceTools."""
    return mock.AsyncMock()


@pytest.fixture
def mcp_handler(
    mock_docker_tools: mock.AsyncMock,
    mock_network_tools: mock.AsyncMock,
    mock_system_tools: mock.AsyncMock,
    mock_logs_tools: mock.AsyncMock,
    mock_workspace_tools: mock.AsyncMock,
) -> MCPHandler:
    """Provide an MCPHandler with all tools mocked."""
    return MCPHandler(
        docker_tools=mock_docker_tools,
        network_tools=mock_network_tools,
        system_tools=mock_system_tools,
        logs_tools=mock_logs_tools,
        workspace_tools=mock_workspace_tools,
    )


# ── TOOL_DEFINITIONS Tests ──────────────────────────────────────────────────────


class TestToolDefinitions:
    """Tests for the TOOL_DEFINITIONS list."""

    def test_tool_definitions_count(self) -> None:
        """TOOL_DEFINITIONS should contain exactly 22 tools."""
        assert len(TOOL_DEFINITIONS) == 22

    def test_all_tool_names_present(self) -> None:
        """All 22 tool names should be present in TOOL_DEFINITIONS."""
        actual_names = {tool.name for tool in TOOL_DEFINITIONS}
        expected_names = set(ALL_TOOL_NAMES)
        assert actual_names == expected_names, (
            f"Missing tools: {expected_names - actual_names}. "
            f"Extra tools: {actual_names - expected_names}"
        )

    def test_tool_definitions_have_required_fields(self) -> None:
        """Each tool should have name, description, and inputSchema."""
        for tool in TOOL_DEFINITIONS:
            assert tool.name, f"Tool at index {TOOL_DEFINITIONS.index(tool)} has no name"
            assert tool.description, f"Tool {tool.name} has no description"
            assert tool.inputSchema is not None, f"Tool {tool.name} has no inputSchema"
            assert isinstance(tool.inputSchema, dict), (
                f"Tool {tool.name} inputSchema is not a dict"
            )
            assert tool.inputSchema.get("type") == "object", (
                f"Tool {tool.name} inputSchema type is not 'object'"
            )

    def test_docker_tools_have_descriptions(self) -> None:
        """All 5 Docker tools should have non-empty descriptions."""
        docker_tools = [
            t for t in TOOL_DEFINITIONS if t.name in DOCKER_TOOL_NAMES
        ]
        assert len(docker_tools) == 5
        for tool in docker_tools:
            assert tool.description, f"Docker tool {tool.name} has empty description"
            assert len(tool.description) > 10, (
                f"Docker tool {tool.name} has suspiciously short description"
            )

    def test_required_params_match_spec(self) -> None:
        """Required parameters should match the spec for key tools."""
        # docker_inspect_container requires "container"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "docker_inspect_container")
        assert "container" in tool.inputSchema.get("required", [])
        assert tool.inputSchema["properties"]["container"]["type"] == "string"

        # docker_container_logs requires "container"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "docker_container_logs")
        assert "container" in tool.inputSchema.get("required", [])
        assert "tail" in tool.inputSchema["properties"]

        # logs_file requires "path"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "logs_file")
        assert "path" in tool.inputSchema.get("required", [])

        # workspace_inspect requires "workspace"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "workspace_inspect")
        assert "workspace" in tool.inputSchema.get("required", [])

        # workspace_docker_ps requires "workspace"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "workspace_docker_ps")
        assert "workspace" in tool.inputSchema.get("required", [])

        # workspace_logs requires "workspace"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "workspace_logs")
        assert "workspace" in tool.inputSchema.get("required", [])

        # workspace_ports requires "workspace"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "workspace_ports")
        assert "workspace" in tool.inputSchema.get("required", [])

        # network_suggest_port requires "preferred_port"
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "network_suggest_port")
        assert "preferred_port" in tool.inputSchema.get("required", [])


# ── MCPHandler.call_tool Tests ─────────────────────────────────────────────────


class TestMCPHandlerCallTool:
    """Tests for MCPHandler.call_tool method."""

    async def test_call_unknown_tool_raises(
        self,
        mcp_handler: MCPHandler,
    ) -> None:
        """Calling an unknown tool should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown tool"):
            await mcp_handler.call_tool("nonexistent_tool", {})

    async def test_call_docker_list_containers(
        self,
        mcp_handler: MCPHandler,
        mock_docker_tools: mock.AsyncMock,
    ) -> None:
        """docker_list_containers should dispatch to DockerTools and return JSON."""
        # Configure the mock to return a success result
        mock_docker_tools.docker_list_containers.return_value = ToolResult(
            data={"containers": [], "total_count": 0, "returned_count": 0}
        )

        result = await mcp_handler.call_tool("docker_list_containers", {})

        # Verify dispatch happened
        mock_docker_tools.docker_list_containers.assert_called_once_with()
        assert len(result) == 1
        assert result[0].type == "text"
        parsed = json.loads(result[0].text)
        assert "data" in parsed
        assert parsed["data"]["total_count"] == 0

    async def test_call_tool_returns_error_json(
        self,
        mcp_handler: MCPHandler,
        mock_docker_tools: mock.AsyncMock,
    ) -> None:
        """When tool returns ToolResult with error, JSON should have {error: {code, message}}."""
        mock_docker_tools.docker_inspect_container.return_value = ToolResult(
            error=ToolError(code="DOCKER_NOT_FOUND", message="Container not found")
        )

        result = await mcp_handler.call_tool(
            "docker_inspect_container", {"container": "missing"}
        )

        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert "error" in parsed
        assert parsed["error"]["code"] == "DOCKER_NOT_FOUND"
        assert parsed["error"]["message"] == "Container not found"

    async def test_call_tool_returns_data_json(
        self,
        mcp_handler: MCPHandler,
        mock_docker_tools: mock.AsyncMock,
    ) -> None:
        """When tool returns ToolResult with data, verify JSON serialization."""
        mock_docker_tools.docker_inspect_container.return_value = ToolResult(
            data={
                "container_id": "abc123",
                "name": "nginx",
                "image": "nginx:latest",
            }
        )

        result = await mcp_handler.call_tool(
            "docker_inspect_container", {"container": "nginx"}
        )

        assert len(result) == 1
        parsed = json.loads(result[0].text)
        assert "data" in parsed
        assert parsed["data"]["container_id"] == "abc123"
        assert parsed["data"]["name"] == "nginx"

    async def test_call_tool_with_arguments(
        self,
        mcp_handler: MCPHandler,
        mock_docker_tools: mock.AsyncMock,
    ) -> None:
        """Arguments should be passed through to the tool method."""
        mock_docker_tools.docker_container_logs.return_value = ToolResult(
            data={"lines": [], "count": 0}
        )

        await mcp_handler.call_tool(
            "docker_container_logs",
            {"container": "nginx", "tail": 50},
        )

        mock_docker_tools.docker_container_logs.assert_called_once_with(
            container="nginx", tail=50
        )

    async def test_call_tool_all_22_dispatched(
        self,
        mcp_handler: MCPHandler,
    ) -> None:
        """All 22 tool names should map to a callable in the dispatch table."""
        dispatch_table = mcp_handler._dispatch
        assert len(dispatch_table) == 22
        for tool_name in ALL_TOOL_NAMES:
            assert tool_name in dispatch_table, f"Missing tool: {tool_name}"
            assert callable(dispatch_table[tool_name]), (
                f"Tool {tool_name} is not callable"
            )


# ── _to_mcp_content Tests ───────────────────────────────────────────────────────


class TestToMcpContent:
    """Tests for MCPHandler._to_mcp_content static method."""

    def test_success_result_serializes_data(self) -> None:
        """ToolResult(data={...}) should serialize to JSON with data key."""
        result = ToolResult(data={"key": "value"})
        content = MCPHandler._to_mcp_content(result)

        assert len(content) == 1
        assert content[0].type == "text"
        parsed = json.loads(content[0].text)
        assert "data" in parsed
        assert parsed["data"]["key"] == "value"

    def test_error_result_serializes_error(self) -> None:
        """ToolResult(error=ToolError(...)) should serialize to JSON with error key."""
        result = ToolResult(
            error=ToolError(code="CONN_FAILED", message="Connection refused")
        )
        content = MCPHandler._to_mcp_content(result)

        assert len(content) == 1
        parsed = json.loads(content[0].text)
        assert "error" in parsed
        assert parsed["error"]["code"] == "CONN_FAILED"
        assert parsed["error"]["message"] == "Connection refused"

    def test_error_result_with_detail(self) -> None:
        """Error with detail dict should include it in output."""
        result = ToolResult(
            error=ToolError(
                code="DOCKER_NOT_FOUND",
                message="Container not found",
                detail={"container": "missing", "searched": True},
            )
        )
        content = MCPHandler._to_mcp_content(result)

        parsed = json.loads(content[0].text)
        assert "detail" in parsed["error"]
        assert parsed["error"]["detail"]["container"] == "missing"
        assert parsed["error"]["detail"]["searched"] is True

    def test_truncated_flag(self) -> None:
        """ToolResult with truncated=True should include truncated in output."""
        result = ToolResult(
            data={"items": ["a", "b", "c"]},
            truncated=True,
        )
        content = MCPHandler._to_mcp_content(result)

        parsed = json.loads(content[0].text)
        assert "truncated" in parsed
        assert parsed["truncated"] is True

    def test_dataclass_serialization(self) -> None:
        """ToolResult with domain model in data uses default=str for serialization."""
        # Simulate a domain model by using a nested dataclass-like structure
        result = ToolResult(
            data={
                "container": {"ID": "abc", "Names": "nginx"},
                "count": 1,
            }
        )
        content = MCPHandler._to_mcp_content(result)

        parsed = json.loads(content[0].text)
        assert "data" in parsed
        assert parsed["data"]["container"]["ID"] == "abc"
        assert parsed["data"]["count"] == 1