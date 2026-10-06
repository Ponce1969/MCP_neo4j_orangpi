"""MCP OranPi server entry point and composition root.

This module creates all dependencies (SSHClient, CommandRunner, WorkspaceResolver,
tool classes) and wires them to the MCP handler. It is the ONLY place where
concrete infrastructure instances are created — all other modules receive
dependencies via constructor injection.

All logs go to stderr (spec 006) so stdout remains exclusively for MCP JSON-RPC.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import mcp.types as types
import structlog
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

from mcp_oranpi.application.docker_tools import DockerTools
from mcp_oranpi.application.logs_tools import LogsTools
from mcp_oranpi.application.network_tools import NetworkTools
from mcp_oranpi.application.system_tools import SystemTools
from mcp_oranpi.application.workspace_tools import WorkspaceTools
from mcp_oranpi.config import AppConfig
from mcp_oranpi.infrastructure.command_runner import CommandRunner
from mcp_oranpi.infrastructure.ssh_client import SSHClient
from mcp_oranpi.infrastructure.workspace_resolver import WorkspaceResolver
from mcp_oranpi.presentation.mcp_handlers import TOOL_DEFINITIONS, MCPHandler


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog to output JSON to stderr.

    Args:
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


# ── App Container ──────────────────────────────────────────────────────────────


class AppContainer:
    """Composition root for the MCP OranPi application.

    Holds all dependencies and provides them to the server.
    This is the ONLY place where concrete infrastructure instances are created.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

        # Infrastructure layer
        self.ssh_client = SSHClient(config.ssh_config)
        self.command_runner = CommandRunner(self.ssh_client)
        self.workspace_resolver = WorkspaceResolver(config, self.ssh_client)

        # Application layer
        self.docker_tools = DockerTools(self.command_runner)
        self.network_tools = NetworkTools(self.command_runner)
        self.system_tools = SystemTools(self.command_runner)
        self.logs_tools = LogsTools(
            self.command_runner,
            allowed_log_dirs=config.allowed_log_dirs_list,
            max_log_file_mb=config.max_log_file_mb,
        )
        self.workspace_tools = WorkspaceTools(
            self.command_runner,
            self.workspace_resolver,
        )

        # Presentation layer
        self.mcp_handler = MCPHandler(
            docker_tools=self.docker_tools,
            network_tools=self.network_tools,
            system_tools=self.system_tools,
            logs_tools=self.logs_tools,
            workspace_tools=self.workspace_tools,
        )

    def create_server(self) -> Server:
        """Create and configure the MCP server.

        Returns:
            A configured Server instance with all routes registered.
        """
        server = Server("mcp-oranpi")

        @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
        async def handle_list_tools() -> list[types.Tool]:
            return TOOL_DEFINITIONS

        @server.call_tool()  # type: ignore[untyped-decorator]
        async def handle_call_tool(
            name: str,
            arguments: dict[str, Any],
        ) -> list[types.TextContent]:
            return await self.mcp_handler.call_tool(name, arguments)

        return server


# ── Server Lifecycle ───────────────────────────────────────────────────────────


async def serve() -> None:
    """Start the MCP OranPi server with stdio transport.

    Loads configuration from environment variables, creates all dependencies,
    connects SSH, loads workspace registry, and serves MCP requests.
    """
    config = AppConfig()  # type: ignore[call-arg]

    configure_logging(config.log_level)

    log = structlog.get_logger()
    log.info("mcp_oranpi_starting", version="0.1.0")

    # Create app container (composition root)
    container = AppContainer(config)

    # Connect SSH
    try:
        await container.ssh_client.connect()
        log.info("ssh_connected", host=config.ssh_host)
    except Exception as exc:
        log.error("ssh_connect_failed", error=str(exc))
        raise

    # Load workspace registry (validates paths on remote host)
    try:
        await container.workspace_resolver.load()
        log.info("workspace_registry_loaded")
    except Exception as exc:
        log.warning("workspace_load_failed", error=str(exc))
        # Non-fatal: workspaces may become available later or on demand

    try:
        server = container.create_server()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await container.ssh_client.disconnect()
        log.info("mcp_oranpi_stopped")


def main() -> None:
    """CLI entry point."""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
